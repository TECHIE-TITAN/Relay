from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from relay.dashboard_api import create_app
from relay.runner import build_arrivals, capacity_rps, load_scenarios, resolve_scenario
from relay.simulations import (
    SIM_PREFIX, SimManager, SimRequest, build_command, build_phases, build_scenario, capacity_per_min, child_env,
)

ROOT = Path(__file__).resolve().parents[1]
DOC = {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200000, "model": "m"}
BASE = yaml.safe_load((ROOT / "config" / "scenarios.yaml").read_text(encoding="utf-8"))


class FakeProc:
    def __init__(self):
        self.pid = 999999999          # never signalled: tests inject a killer that only flips the flag
        self.rc = None

    def poll(self):
        return self.rc

    def terminate(self):
        self.rc = -15


def manager(tmp_path, procs, **kw):
    def launcher(cmd, env, cwd, log):
        p = FakeProc()
        procs.append((cmd, env, cwd, p))
        return p
    return SimManager(tmp_path / "results", tmp_path, DOC, scenarios_file=ROOT / "config" / "scenarios.yaml",
                      launcher=launcher, killer=lambda p: p.terminate(), **kw)


def req(**kw):
    return SimRequest(**{"pattern": "steady", **kw})


def test_capacity_arithmetic_matches_the_runners():
    for scale in (1, 2, 5, 10):
        used = {"rpm": 30 * scale, "tpm": 8000 * scale}
        assert abs(capacity_per_min(DOC, scale, 2200) - 60 * capacity_rps(used, 2200)) < 1e-9


def test_every_pattern_becomes_phases_the_runner_understands():
    for pattern, rate in (("steady", 0.7), ("burst", 0.5), ("ramp", 1.0), ("sustained_overload", 2.5)):
        r = req(pattern=pattern, rate_x=rate, duration_s=100)
        phases = build_phases(r)
        assert abs(sum(p["duration_s"] for p in phases) - 100) < 1e-6
        assert phases[0]["start_s"] == 0 and all(p["rate_x"] > 0 for p in phases)
        # contiguous: each phase starts where the previous one ends
        for a, b in zip(phases, phases[1:]):
            assert abs(a["start_s"] + a["duration_s"] - b["start_s"]) < 1e-6
        assert build_arrivals(phases, capacity=0.3, seed=1)             # the real arrival generator accepts them
    burst = build_phases(req(pattern="burst", rate_x=0.5, burst_x=10, duration_s=100))
    assert [p["rate_x"] for p in burst] == [0.5, 10.0, 0.5]
    ramp = build_phases(req(pattern="ramp", rate_x=1.0, duration_s=100))
    assert [p["rate_x"] for p in ramp] == [0.2, 0.4, 0.6, 0.8, 1.0]


def test_impossible_or_out_of_range_requests_are_rejected():
    with pytest.raises(ValidationError):
        req(rate_x=50)
    with pytest.raises(ValidationError):
        req(concurrency=0)
    with pytest.raises(ValidationError):
        req(duration_s=5)
    with pytest.raises(ValidationError):
        SimRequest(pattern="tsunami")
    with pytest.raises(ValueError, match="at least 1.5"):
        build_phases(req(pattern="sustained_overload", rate_x=1.0))
    with pytest.raises(ValueError, match="too short"):
        build_phases(req(pattern="burst", duration_s=20))


def test_the_scenario_is_a_plain_standin_scenario_the_runner_can_resolve():
    r = req(pattern="burst", rate_x=0.6, duration_s=120, concurrency=6, limits_scale=3.0,
            whatif={"halve_limits_at_s": 60, "slow_p": 0.1, "slow_factor": 4.0, "retry_policy": "budgeted"})
    name, scn = build_scenario(r, BASE)
    assert name == "sim_burst" and name.startswith(SIM_PREFIX)
    assert scn["source"] == "standin" and "max_requests" not in scn and scn["limits_scale"] == 3.0
    assert scn["admission"] == {"n_inflight": 6}
    assert scn["standin"] == {"p_slow": 0.1, "slow_factor": 4.0}
    assert scn["controls"] == [{"at_s": 60, "set": {"rpm": "x0.5", "tpm": "x0.5"}}]
    resolved = resolve_scenario({"defaults": BASE["defaults"], "scenarios": {name: scn}}, name)      # the runner's own merge
    assert resolved["admission"]["queue_cap"] == 40 and resolved["admission"]["n_inflight"] == 6
    assert resolved["source"] == "standin"
    # a halving that would happen after the run ends is simply not scheduled
    assert build_scenario(req(duration_s=60, whatif={"halve_limits_at_s": 100}), BASE)[1]["controls"] == []


def test_the_naive_policy_is_the_one_defined_in_scenarios_yaml():
    name, scn = build_scenario(req(whatif={"retry_policy": "naive"}), BASE)
    naive = BASE["scenarios"]["retry_naive"]
    assert name == "sim_steady_naive"
    for k in ("gateway", "admission", "pipeline"):
        assert scn[k] == naive[k]
    assert scn["gateway"]["limiter_enabled"] is False                      # taken from the file, not re-typed here
    budgeted = build_scenario(req(), BASE)[1]
    assert "gateway" not in budgeted and "pipeline" not in budgeted


def test_the_command_never_allows_the_real_service_and_the_environment_has_no_key(tmp_path):
    cmd = build_command("sim_x", "sim_steady", tmp_path / "s.yaml", 8123)
    assert cmd[1:4] == ["-m", "relay.cli", "run"] and "--spawn-standin" in cmd
    assert "--allow-real" not in cmd and "--run-id" in cmd and cmd[cmd.index("--port") + 1] == "8123"
    env = child_env(tmp_path, {"GROQ_API_KEY": "gsk_secret", "groq_other": "x", "PATH": "/bin", "HOME": "/h"})
    assert "GROQ_API_KEY" not in env and "groq_other" not in env and env["PATH"] == "/bin" and env["RELAY_ROOT"] == str(tmp_path)


def test_start_writes_the_scenario_file_launches_it_and_limits_concurrency(tmp_path):
    procs: list = []
    m = manager(tmp_path, procs, max_running=2)
    a = m.start(req())
    b = m.start(req(pattern="ramp"))
    assert a["status"] == "running" and a["run_id"].startswith("sim_steady_") and a["capacity_per_min"] > 0 and a["expected_requests"] > 0
    cmd, env, cwd, _ = procs[0]
    assert (tmp_path / "results" / ".sim" / f"{a['run_id']}.yaml").is_file() and "GROQ_API_KEY" not in env and cwd == str(tmp_path)
    written = yaml.safe_load((tmp_path / "results" / ".sim" / f"{a['run_id']}.yaml").read_text(encoding="utf-8"))
    assert list(written["scenarios"]) == ["sim_steady"] and written["scenarios"]["sim_steady"]["source"] == "standin"
    with pytest.raises(RuntimeError, match="already running"):
        m.start(req())
    m.stop(a["sim_id"])
    assert [s["status"] for s in m.list() if s["sim_id"] == a["sim_id"]] == ["stopped"]
    assert m.start(req())["status"] == "running"                         # a slot freed up
    assert b["run_id"] != a["run_id"]


def test_status_follows_the_process_and_a_failure_shows_its_log(tmp_path):
    procs: list = []
    m = manager(tmp_path, procs)
    s = m.start(req())
    procs[0][3].rc = 0
    assert m.list()[0]["status"] == "finished"
    t = m.start(req(pattern="ramp"))
    (tmp_path / "results" / ".sim" / f"{t['run_id']}.log").write_text("Traceback ... boom", encoding="utf-8")
    procs[1][3].rc = 1
    bad = [x for x in m.list() if x["sim_id"] == t["sim_id"]][0]
    assert bad["status"] == "failed" and "boom" in bad["log_tail"] and s["sim_id"] != t["sim_id"]


# ---------------------------------------------------------------------------- over HTTP

def client(tmp_path, procs, enabled=True):
    app = create_app(tmp_path / "results", root=tmp_path, documented_limits=DOC, prices={}, enable_simulations=enabled,
                     sim_manager=manager(tmp_path, procs) if enabled else None)
    return TestClient(app)


def test_simulation_routes_exist_only_when_enabled(tmp_path):
    off = client(tmp_path, [], enabled=False)
    assert off.get("/api/simulations/options").status_code == 404 and off.post("/api/simulations", json={"pattern": "steady"}).status_code == 404
    assert off.get("/api/health").json()["simulations_enabled"] is False
    on = client(tmp_path / "on", [])
    assert on.get("/api/health").json()["simulations_enabled"] is True


def test_http_options_start_stop_and_validation(tmp_path):
    procs: list = []
    c = client(tmp_path, procs)
    o = c.get("/api/simulations/options").json()
    assert [p["id"] for p in o["patterns"]] == ["steady", "burst", "ramp", "sustained_overload"] and "SIMULATED" in o["provenance"]
    assert o["capacity_per_min"]["5"] > 0 and o["max_running"] == 2
    r = c.post("/api/simulations", json={"pattern": "burst", "rate_x": 0.5, "duration_s": 100, "concurrency": 4})
    assert r.status_code == 201 and r.json()["scenario"] == "sim_burst" and r.json()["status"] == "running"
    assert c.post("/api/simulations", json={"pattern": "steady", "rate_x": 99}).status_code == 422          # out of range
    assert c.post("/api/simulations", json={"pattern": "sustained_overload", "rate_x": 1.0}).status_code == 422
    assert c.post("/api/simulations", json={"pattern": "steady"}).status_code == 201
    assert c.post("/api/simulations", json={"pattern": "steady"}).status_code == 409                          # two already running
    sid = r.json()["sim_id"]
    assert c.post(f"/api/simulations/{sid}/stop").json()["status"] == "stopped"
    assert c.post("/api/simulations/nope/stop").status_code == 404
    assert len(c.get("/api/simulations").json()) == 2
