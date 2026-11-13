"""Start a STAND-IN simulation from the dashboard by launching the existing runner.

There is no second simulation engine here. A request body is validated against an allowlist, turned into an
ordinary scenario (the same keys scenarios.yaml uses), written to results/.sim/<run_id>.yaml, and run with

    python -m relay.cli run --scenario NAME --scenarios-file FILE --spawn-standin --port P --run-id RUN_ID

so the events the dashboard shows are the ones the real runner writes. Guarantees (each has a test):
  * source is always the stand-in; `--allow-real` is never passed; no max_requests; limits_scale is simulated;
  * every GROQ* variable is removed from the child's environment, so it could not reach the provider even by mistake;
  * at most `max_running` simulations at once (they share this machine's CPU, and timings are only comparable then);
  * runs are named sim_* so `analyze` never merges them into the evidence (results.csv).
"""
from __future__ import annotations

import copy
import os
import signal
import socket
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field
from typing import Literal

SIM_PREFIX = "sim_"

PATTERNS = [
    {"id": "steady", "label": "Steady", "description": "Constant Poisson load at the chosen rate."},
    {"id": "burst", "label": "Burst", "description": "Calm, then a short burst at the burst multiple, then calm again."},
    {"id": "ramp", "label": "Ramp", "description": "Five equal steps rising from one fifth of the chosen rate up to it (a stepped ramp: the runner has piecewise-constant rates)."},
    {"id": "sustained_overload", "label": "Sustained overload", "description": "A constant rate of at least 1.5 times capacity for the whole run."},
]


class WhatIf(BaseModel):
    halve_limits_at_s: Optional[int] = Field(None, ge=10, le=290)      # the stand-in halves its RPM and TPM at this time
    slow_p: float = Field(0.0, ge=0.0, le=0.3)                          # share of calls that succeed slowly (SIMULATED)
    slow_factor: float = Field(6.0, ge=2.0, le=10.0)
    retry_policy: Literal["budgeted", "naive"] = "budgeted"


class SimRequest(BaseModel):
    pattern: Literal["steady", "burst", "ramp", "sustained_overload"]
    rate_x: float = Field(0.7, ge=0.1, le=4.0)           # multiple of the arithmetic capacity (see options)
    burst_x: float = Field(10.0, ge=2.0, le=12.0)         # used by the burst pattern only
    duration_s: int = Field(90, ge=20, le=300)
    concurrency: int = Field(4, ge=1, le=16)              # admission n_inflight (ignored by the naive policy, which has none)
    limits_scale: float = Field(5.0, ge=1.0, le=10.0)     # SIMULATED: documented limits times this
    deadline_s: float = Field(20.0, ge=5.0, le=60.0)
    seed: int = Field(1, ge=0, le=1000000)
    whatif: WhatIf = Field(default_factory=WhatIf)


def capacity_per_min(documented: Dict[str, Any], scale: float, est_tokens: float = 2200.0) -> float:
    """Answers per minute the limits allow (same arithmetic as runner.capacity_rps; a test keeps them equal)."""
    rpm, tpm = documented["rpm"] * scale, documented["tpm"] * scale
    return 60.0 * min(rpm / 60.0, tpm / 60.0 / max(1.0, est_tokens))


def build_phases(req: SimRequest) -> List[dict]:
    d = float(req.duration_s)
    if req.pattern in ("steady", "sustained_overload"):
        if req.pattern == "sustained_overload" and req.rate_x < 1.5:
            raise ValueError("sustained overload needs a rate of at least 1.5 times capacity")
        return [{"start_s": 0, "duration_s": d, "rate_x": req.rate_x}]
    if req.pattern == "burst":
        calm1 = round(d * 0.4)
        burst = max(10.0, round(d * 0.15))
        calm2 = d - calm1 - burst
        if calm2 < 5:
            raise ValueError("duration too short for a burst: use at least 40 seconds")
        return [{"start_s": 0, "duration_s": calm1, "rate_x": req.rate_x},
                {"start_s": calm1, "duration_s": burst, "rate_x": req.burst_x},
                {"start_s": calm1 + burst, "duration_s": calm2, "rate_x": req.rate_x}]
    step = d / 5.0
    return [{"start_s": round(step * k, 3), "duration_s": round(step, 3), "rate_x": round(req.rate_x * (k + 1) / 5.0, 4)} for k in range(5)]


def build_scenario(req: SimRequest, base_doc: Dict[str, Any]) -> tuple:
    """(scenario name, scenario dict) using only keys the runner already understands."""
    naive = req.whatif.retry_policy == "naive"
    name = f"{SIM_PREFIX}{req.pattern}{'_naive' if naive else ''}"
    scn: Dict[str, Any] = {
        "description": (f"Started from the dashboard (SIMULATED stand-in): {req.pattern}, {req.rate_x}x capacity, {req.duration_s} s, "
                        f"concurrency {req.concurrency}, retry policy {req.whatif.retry_policy}."),
        "source": "standin",
        "phases": build_phases(req),
        "seed": req.seed,
        "deadline_s": req.deadline_s,
        "limits_scale": req.limits_scale,
        "drain_s": 10,
        "admission": {"n_inflight": req.concurrency},
        "standin": {},
        "controls": [],
    }
    if req.whatif.slow_p > 0:
        scn["standin"] = {"p_slow": req.whatif.slow_p, "slow_factor": req.whatif.slow_factor}
    if req.whatif.halve_limits_at_s is not None and req.whatif.halve_limits_at_s < req.duration_s:
        scn["controls"] = [{"at_s": req.whatif.halve_limits_at_s, "set": {"rpm": "x0.5", "tpm": "x0.5"}}]
    if naive:                      # the naive blocks come from scenarios.yaml itself: one definition, not a copy
        src = (base_doc.get("scenarios") or {}).get("retry_naive") or {}
        for k in ("gateway", "admission", "pipeline"):
            if k in src:
                scn[k] = copy.deepcopy(src[k])
    return name, scn


def build_command(run_id: str, name: str, scenario_file: Path, port: int) -> List[str]:
    return [sys.executable, "-m", "relay.cli", "run", "--scenario", name, "--scenarios-file", str(scenario_file),
            "--spawn-standin", "--port", str(port), "--run-id", run_id]


def child_env(root: Path, environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {k: v for k, v in (environ if environ is not None else os.environ).items() if not k.upper().startswith("GROQ")}
    env["RELAY_ROOT"] = str(root)
    return env


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def default_launcher(cmd: List[str], env: Dict[str, str], cwd: str, log) -> Any:
    # its own session, so stopping it can signal the runner AND the stand-in server it spawned
    return subprocess.Popen(cmd, env=env, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def default_killer(proc: Any) -> None:
    """Signal the whole process group (the runner and the stand-in server it spawned)."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        try:
            proc.terminate()
        except Exception:
            pass


@dataclass
class Sim:
    sim_id: str
    run_id: str
    scenario: str
    spec: dict
    started_at: str
    proc: Any
    log_path: Path
    port: int
    stopped: bool = False


class SimManager:
    def __init__(self, results_dir: Path, root: Path, documented_limits: Dict[str, Any], *, scenarios_file: Optional[Path] = None,
                 launcher: Callable[..., Any] = default_launcher, killer: Callable[[Any], None] = default_killer,
                 max_running: int = 2) -> None:
        self.killer = killer
        self.results_dir = results_dir
        self.root = root
        self.documented = documented_limits
        self.scenarios_file = scenarios_file or (root / "config" / "scenarios.yaml")
        self.launcher = launcher
        self.max_running = max_running
        self.sims: Dict[str, Sim] = {}

    # ------------------------------------------------------------------ options (what the form may offer)
    def base_doc(self) -> Dict[str, Any]:
        return yaml.safe_load(self.scenarios_file.read_text(encoding="utf-8")) or {}

    def options(self) -> dict:
        est = float((self.base_doc().get("defaults") or {}).get("est_tokens_per_answer", 2200))
        return {
            "patterns": PATTERNS,
            "limits": {
                "rate_x": {"min": 0.1, "max": 4.0, "default": 0.7, "unit": "times capacity"},
                "burst_x": {"min": 2.0, "max": 12.0, "default": 10.0},
                "duration_s": {"min": 20, "max": 300, "default": 90},
                "concurrency": {"min": 1, "max": 16, "default": 4, "meaning": "admission n_inflight: requests in the pipeline at once"},
                "limits_scale": {"min": 1.0, "max": 10.0, "default": 5.0, "meaning": "SIMULATED: the documented limits times this"},
                "deadline_s": {"min": 5.0, "max": 60.0, "default": 20.0, "meaning": "how long a request waits before it is given up"},
                "halve_limits_at_s": {"min": 10, "max": 290},
                "slow_p": {"min": 0.0, "max": 0.3}, "slow_factor": {"min": 2.0, "max": 10.0},
            },
            "retry_policies": [{"id": "budgeted", "label": "Budgeted (the default policy)"},
                               {"id": "naive", "label": "Naive: no limiter, immediate retries, unbounded concurrency (as in retry_naive)"}],
            "capacity_per_min": {str(s): round(capacity_per_min(self.documented, s, est), 2) for s in (1, 2, 5, 10)},
            "est_tokens_per_answer": est,
            "max_running": self.max_running,
            "provenance": "SIMULATED: every simulation runs against the stand-in. It never calls the real service and uses no quota.",
        }

    # ------------------------------------------------------------------ lifecycle
    def _status(self, s: Sim) -> dict:
        rc = s.proc.poll()
        status = "running" if rc is None else ("stopped" if s.stopped else ("finished" if rc == 0 else "failed"))
        out = {"sim_id": s.sim_id, "run_id": s.run_id, "scenario": s.scenario, "status": status, "returncode": rc,
               "started_at": s.started_at, "spec": s.spec}
        if status == "failed":
            try:
                out["log_tail"] = s.log_path.read_text(encoding="utf-8", errors="replace")[-800:]
            except OSError:
                out["log_tail"] = ""
        return out

    def list(self) -> List[dict]:
        return [self._status(s) for s in sorted(self.sims.values(), key=lambda x: x.started_at, reverse=True)]

    def running(self) -> int:
        return sum(1 for s in self.sims.values() if s.proc.poll() is None)

    def start(self, req: SimRequest) -> dict:
        if self.running() >= self.max_running:
            raise RuntimeError(f"{self.max_running} simulations are already running; wait for one to finish or stop it")
        name, scn = build_scenario(req, self.base_doc())
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        sim_id = uuid.uuid4().hex[:6]
        run_id = f"{name}_{stamp}_{sim_id}"
        sim_dir = self.results_dir / ".sim"
        sim_dir.mkdir(parents=True, exist_ok=True)
        scenario_file = sim_dir / f"{run_id}.yaml"
        doc = {"defaults": (self.base_doc().get("defaults") or {}), "scenarios": {name: scn}}
        scenario_file.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        port = free_port()
        log_path = sim_dir / f"{run_id}.log"
        with open(log_path, "wb") as log:
            proc = self.launcher(build_command(run_id, name, scenario_file, port), child_env(self.root), str(self.root), log)
        spec = req.model_dump() if hasattr(req, "model_dump") else req.dict()
        sim = Sim(sim_id=sim_id, run_id=run_id, scenario=name, spec=spec, started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  proc=proc, log_path=log_path, port=port)
        self.sims[sim_id] = sim
        cap = capacity_per_min(self.documented, req.limits_scale, float((self.base_doc().get("defaults") or {}).get("est_tokens_per_answer", 2200)))
        expected = sum(p["duration_s"] * p["rate_x"] for p in scn["phases"]) * cap / 60.0
        return {**self._status(sim), "capacity_per_min": round(cap, 2), "expected_requests": round(expected)}

    def stop(self, sim_id: str) -> dict:
        s = self.sims.get(sim_id)
        if s is None:
            raise KeyError(sim_id)
        if s.proc.poll() is None:
            s.stopped = True
            self.killer(s.proc)
        return self._status(s)

    def shutdown(self) -> None:
        for sid in list(self.sims):
            try:
                self.stop(sid)
            except Exception:
                pass
