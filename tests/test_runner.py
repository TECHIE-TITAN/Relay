from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
import yaml

from relay.artifacts import RESULT_COLUMNS, read_results_csv, run_paths
from relay.chunk import write_chunks
from relay.config import load_config
from relay.runner import build_arrivals, capacity_rps, load_scenarios, resolve_control, resolve_scenario, run_scenario
from relay.schema import Chunk, RelayError
from relay.standin.profile import StandinProfile
from relay.standin.server import create_app


def test_shipped_scenarios_parse_and_resolve():
    root = Path(__file__).resolve().parents[1]
    doc = load_scenarios(root / "config" / "scenarios.yaml")
    names = set(doc["scenarios"])
    assert {"steady", "burst", "sustained_overload", "limit_halved", "daily_exhausted",
            "retry_naive", "retry_budgeted", "smoke"} <= names
    naive = resolve_scenario(doc, "retry_naive")
    assert naive["gateway"]["limiter_enabled"] is False and naive["admission"]["queue_cap"] == 100000
    assert naive["limits_scale"] == 5 and naive["name"] == "retry_naive"        # inherited default
    assert resolve_scenario(doc, "quality_S2")["source"] == "real"
    with pytest.raises(RelayError):
        resolve_scenario(doc, "nope")


def test_scenario_merge_is_per_key_for_mappings():
    doc = {"defaults": {"admission": {"n_inflight": 4, "queue_cap": 40}, "seed": 1},
           "scenarios": {"a": {"admission": {"queue_cap": 5}}}}
    s = resolve_scenario(doc, "a")
    assert s["admission"] == {"n_inflight": 4, "queue_cap": 5} and s["seed"] == 1


def test_arrivals_are_seeded_sorted_and_respect_phases():
    phases = [{"start_s": 0, "duration_s": 100, "rate_rps": 2.0}, {"start_s": 100, "duration_s": 50, "rate_rps": 0},
              {"start_s": 150, "duration_s": 50, "rate_x": 1.0}]
    a = build_arrivals(phases, capacity=4.0, seed=7)
    assert a == build_arrivals(phases, capacity=4.0, seed=7) and a == sorted(a)
    assert not any(100 <= t < 150 for t in a)
    assert 150 < sum(1 for t in a if t < 100) < 250          # ~200 expected
    assert 130 < sum(1 for t in a if t >= 150) < 270         # ~200 expected
    assert a != build_arrivals(phases, capacity=4.0, seed=8)


def test_capacity_is_the_binding_limit_arithmetic():
    assert abs(capacity_rps({"rpm": 30, "tpm": 8000}, 2200) - 8000 / 60 / 2200) < 1e-12     # tokens bind
    assert capacity_rps({"rpm": 30, "tpm": 800000}, 2200) == 0.5                              # requests bind


def test_control_multipliers_resolve_against_the_scaled_limits():
    used = {"rpm": 150, "tpm": 40000, "rpd": 5000, "tpd": 1000000}
    assert resolve_control({"rpm": "x0.5", "tpm": "x0.5", "tpd": 1, "p_5xx": 0.3}, used) == {
        "rpm": 75, "tpm": 20000, "tpd": 1, "p_5xx": 0.3}


# ---------------------------------------------------------------- end to end against a live local stand-in

QUESTIONS = """\
version: 1
questions:
  - {id: q01, text: "How many identical layers are in the encoder stack?", answerable: true, category: single-fact, gold: [{paper: attention, pages: [3]}]}
  - {id: q02, text: "Who won the football match yesterday?", answerable: false, category: unanswerable}
"""
TEXT = "The encoder is composed of a stack of N = 6 identical layers. Each layer has two sub-layers."


@pytest.fixture
def live_project(project: Path):
    (project / "data" / "processed").mkdir(parents=True)
    write_chunks(project / "data" / "processed" / "chunks_S1.jsonl", [
        Chunk(chunk_id="a1", paper_key="attention", setting_id="S1", page_start=3, page_end=3, section="3.1",
              text=TEXT, approx_tokens=len(TEXT) // 4, printed_start="2", printed_end="2"),
    ])
    (project / "data" / "questions.yaml").write_text(QUESTIONS, encoding="utf-8")
    return project


@pytest.fixture
def standin_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(create_app(StandinProfile()), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    t0 = time.time()
    while not server.started:
        if time.time() - t0 > 10:
            raise RuntimeError("stand-in test server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)


TINY = {
    "name": "tiny", "source": "standin", "setting": "S1", "seed": 3, "deadline_s": 6, "limits_scale": 10,
    "est_tokens_per_answer": 500, "drain_s": 4, "state_interval_s": 0.2,
    "standin": {"ttft_median_ms": 20, "ttft_sigma": 0.0, "tokens_per_s": 5000, "p_5xx": 0.0, "p_hang": 0.0,
                "p_slow": 0.0, "p_midstream": 0.0, "p_bad_citation": 0.0},
    "admission": {"n_inflight": 3, "queue_cap": 10, "min_service_s": 0.2},
    "pipeline": {"ladder_enabled": False},
    "phases": [{"start_s": 0, "duration_s": 2, "rate_rps": 3}],
}


def test_end_to_end_stand_in_run_writes_the_three_artifacts(live_project, standin_url):
    cfg = load_config(live_project)
    res = asyncio.run(run_scenario(cfg, dict(TINY), run_id="tiny_run", standin_url=standin_url))
    paths = run_paths(cfg.paths.results_dir, "tiny_run")
    assert paths.manifest.is_file() and paths.events.is_file() and paths.results.is_file()

    rows = read_results_csv(paths.results)
    assert list(rows[0].keys()) == list(RESULT_COLUMNS) and len(rows) == res.n_rows > 0
    assert {r["status"] for r in rows} <= {"ok", "rate_limited", "timeout", "error"}
    assert all(r["source"] == "stand-in" and r["answer_grade"] == "" for r in rows)
    assert sum(1 for r in rows if r["status"] == "ok") >= 1
    assert all(r["first_token_ms"] != "" for r in rows if r["status"] == "ok")

    events = [json.loads(line) for line in paths.events.read_text(encoding="utf-8").splitlines()]
    per = {}
    for e in events:
        if e["request_id"]:
            per.setdefault(e["request_id"], []).append(e["event"])
    assert len(per) == res.n_rows and all(v.count("done") == 1 for v in per.values())
    assert any(e["event"] == "gateway_state" for e in events)
    assert {"run_start", "run_end"} <= {e["event"] for e in events}
    assert all(e["lag_ms"] >= 0 for e in events if e["event"] == "launch")

    manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    assert manifest["source"] == "stand-in" and manifest["limits"]["used_provenance"] == "SIMULATED"
    assert manifest["limits"]["used"]["tpm"] == 80000 and manifest["limits"]["documented"]["tpm"] == 8000
    assert manifest["standin_stats_at_end"]["counts"]["received"] >= 1
    assert all(r["passed"] for r in res.invariants if r["applicable"]), res.invariants
    assert "No quality number" in manifest["note"]


def test_a_real_scenario_is_refused_without_allow_real_and_creates_nothing(live_project):
    cfg = load_config(live_project)
    scn = {**TINY, "name": "q", "source": "real", "max_requests": 2, "limits_scale": 1}
    with pytest.raises(RelayError, match="--allow-real"):
        asyncio.run(run_scenario(cfg, scn))
    assert not (cfg.paths.results_dir / "runs").exists()
    with pytest.raises(RelayError, match="max_requests"):
        asyncio.run(run_scenario(cfg, {**scn, "max_requests": None}, allow_real=True))
    with pytest.raises(RelayError, match="limits_scale"):
        asyncio.run(run_scenario(cfg, {**scn, "limits_scale": 5}, allow_real=True))


def test_stand_in_run_needs_a_server_and_run_ids_cannot_escape(live_project):
    cfg = load_config(live_project)
    with pytest.raises(RelayError, match="standin"):
        asyncio.run(run_scenario(cfg, dict(TINY)))
    with pytest.raises(RelayError, match="invalid run id"):
        run_paths(cfg.paths.results_dir, "../escape")
    with pytest.raises(RelayError):
        run_paths(cfg.paths.results_dir, "a/b")
