"""Experiment runner: open-loop load against the stand-in (or, deliberately, the real service).

    python -m relay.cli run --scenario steady --spawn-standin

Independent of any dashboard: it writes results/runs/<run_id>/{manifest.json, events.jsonl,
results.csv} and imports nothing from a UI layer.

* Open loop. Arrival times are drawn up front from a seeded Poisson schedule and each request is
  launched at its scheduled time whatever the system is doing (no coordinated omission). The
  scheduling lag of every launch is logged (`launch.lag_ms`) so a lagging generator is visible.
* The stand-in is configured by the scenario at start via /control (limits = documented x
  limits_scale, plus overrides) and mid-run by `controls`; the server's own counters are saved.
* source=real is refused unless --allow-real is given, needs `max_requests`, and is capped by the
  daily ledger. The runner never reads a key for a stand-in run.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import platform
import random
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

import httpx
import yaml

from .admission import Admission
from .artifacts import RunPaths, new_run_id, read_events, run_paths, write_manifest, write_results_csv
from .config import Config, get_api_key
from .eval_retrieval import load_questions
from .gateway import DailyLedger, Gateway, GatewayConfig
from .llm_client import LLMClient
from .metrics import check_invariants, csv_rows, request_rows, summarize
from .pipeline import Pipeline, PipelineConfig
from .retrieve import Retriever
from .schema import RelayError
from .tracing import EventLog, RequestContext

STANDIN_LIMIT_KEYS = ("rpm", "tpm", "rpd", "tpd")


# ----------------------------------------------------------------------------- scenarios

def load_scenarios(path: Path) -> dict:
    if not path.is_file():
        raise RelayError(f"{path} not found")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("scenarios"), dict):
        raise RelayError(f"{path}: expected a 'scenarios' mapping")
    return doc


def resolve_scenario(doc: Mapping[str, Any], name: str) -> dict:
    scenarios = doc.get("scenarios") or {}
    if name not in scenarios:
        raise RelayError(f"unknown scenario '{name}'. Known: {sorted(scenarios)}")
    merged = copy.deepcopy(doc.get("defaults") or {})
    for k, v in (scenarios[name] or {}).items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    merged["name"] = name
    return merged


def capacity_rps(limits: Mapping[str, int], est_tokens: float) -> float:
    """Answers per second the limits allow. Arithmetic on the limits, not a measurement."""
    return min(limits["rpm"] / 60.0, limits["tpm"] / 60.0 / max(1.0, est_tokens))


def build_arrivals(phases: list, capacity: float, seed: int) -> list:
    """Seeded Poisson arrival offsets (seconds from start) for piecewise-constant rates."""
    rng = random.Random(seed)
    out: list = []
    for ph in phases:
        start, dur = float(ph["start_s"]), float(ph["duration_s"])
        rate = float(ph["rate_rps"]) if "rate_rps" in ph else float(ph["rate_x"]) * capacity
        if rate <= 0:
            continue
        t = start
        while True:
            t += rng.expovariate(rate)
            if t >= start + dur:
                break
            out.append(t)
    return sorted(out)


def resolve_control(setting: Mapping[str, Any], limits_used: Mapping[str, int]) -> dict:
    """'x0.5' means 0.5 times the scaled limit currently in force; anything else is literal."""
    out = {}
    for k, v in setting.items():
        if isinstance(v, str) and v.startswith("x") and k in limits_used:
            out[k] = int(round(limits_used[k] * float(v[1:])))
        else:
            out[k] = v
    return out


def _build(cls, kwargs: Mapping[str, Any], what: str):
    try:
        return cls(**kwargs)
    except TypeError as e:
        raise RelayError(f"scenario option error in {what}: {e}") from e


# ----------------------------------------------------------------------------- stand-in helpers

async def wait_healthy(url: str, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=2.0) as c:
        while True:
            try:
                r = await c.get(f"{url}/health")
                if r.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RelayError(
                    f"no stand-in server answering at {url}. Start one with `python -m relay.cli standin` "
                    f"or pass --spawn-standin."
                )
            await asyncio.sleep(0.2)


def spawn_standin(port: int) -> "subprocess.Popen":
    return subprocess.Popen(
        [sys.executable, "-m", "relay.standin.server", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ----------------------------------------------------------------------------- the run

@dataclass
class RunResult:
    run_id: str
    paths: RunPaths
    summary: dict
    invariants: list
    n_rows: int


async def _sampler(log: EventLog, gw: Gateway, adm: Admission, interval: float) -> None:
    while True:
        snap = adm.snapshot()
        log.emit(None, "gateway", "gateway_state", queue_len=snap["queue_len"], inflight=snap["inflight"],
                 peak_queue=snap["peak_queue"], service_estimate_s=snap["service_estimate_s"], **gw.snapshot())
        await asyncio.sleep(interval)


async def _control(http: httpx.AsyncClient, url: str, at_s: float, body: dict, t_start: float, log: EventLog) -> None:
    await asyncio.sleep(max(0.0, t_start + at_s - time.monotonic()))
    ok = False
    try:
        r = await http.post(f"{url}/control", json=body)
        ok = r.status_code == 200
    except httpx.HTTPError:
        pass
    log.emit(None, "runner", "control", at_s=at_s, body=body, ok=ok, source_note="SIMULATED stand-in control")


async def run_scenario(cfg: Config, scn: Mapping[str, Any], *, run_id: Optional[str] = None,
                       standin_url: Optional[str] = None, allow_real: bool = False,
                       scenarios_file: str = "") -> RunResult:
    name = scn["name"]
    source = str(scn.get("source", "standin"))
    real = source == "real"
    if source not in ("standin", "real"):
        raise RelayError(f"scenario {name}: source must be 'standin' or 'real'")
    if real:
        if not allow_real:
            raise RelayError(
                f"scenario '{name}' uses the REAL service and spends quota. Re-run with --allow-real "
                f"after checking the plan with you. Nothing was sent."
            )
        if not scn.get("max_requests"):
            raise RelayError(f"scenario '{name}': real runs need max_requests")
        if float(scn.get("limits_scale", 1)) != 1.0:
            raise RelayError(f"scenario '{name}': limits_scale must be 1 for a real run")
    elif not standin_url:
        raise RelayError("a stand-in run needs --standin-url or --spawn-standin")

    model = cfg.model()
    scale = float(scn.get("limits_scale", 1.0))
    documented = {"rpm": model.limits.rpm, "rpd": model.limits.rpd, "tpm": model.limits.tpm, "tpd": model.limits.tpd}
    used = {k: int(round(v * scale)) for k, v in documented.items()}

    run_id = run_id or new_run_id(name)
    paths = run_paths(cfg.paths.results_dir, run_id)
    if paths.root.exists():
        raise RelayError(f"{paths.root} already exists. Pick another --run-id.")
    paths.root.mkdir(parents=True)

    q_all = load_questions(cfg.paths.questions, [p.key for p in cfg.papers])
    setting = str(scn.get("setting", cfg.default_setting))
    retrievers = {setting: Retriever.from_config(cfg, setting)}
    deadline_s = float(scn.get("deadline_s", 20))
    seed = int(scn.get("seed", 1))

    # --- client
    http = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0), limits=httpx.Limits(max_connections=2000, max_keepalive_connections=200))
    standin_profile_before = None
    if real:
        key = get_api_key(cfg.root)
        if not key:
            raise RelayError("GROQ_API_KEY is not set (put it in .env). Nothing was sent.")
        client = LLMClient(model.base_url, model.id, api_key=key, client=http, extra_params=model.extra_params,
                           stream_options=bool(scn.get("stream_options", False)))
    else:
        await wait_healthy(standin_url)    # type: ignore[arg-type]
        body = {"reset": True, "set": {**used, **(scn.get("standin") or {})}}
        r = await http.post(f"{standin_url}/control", json=body)
        if r.status_code != 200:
            raise RelayError(f"stand-in rejected the scenario's profile: {r.text[:200]}")
        standin_profile_before = r.json().get("profile")
        client = LLMClient(f"{standin_url}/v1", model.id, client=http, stream_options=bool(scn.get("stream_options", True)))

    # --- gateway / admission / pipeline
    g_kwargs: dict[str, Any] = {"rpm": used["rpm"], "tpm": used["tpm"]}
    if not real:
        g_kwargs.update(daily_token_cap=10 ** 12, daily_request_cap=10 ** 9)   # the stand-in's own limit is the signal
    g_kwargs.update(scn.get("gateway") or {})
    gcfg = _build(GatewayConfig, g_kwargs, "gateway")
    p_kwargs = {"setting": setting, "deadline_s": deadline_s, "seed": seed, **(scn.get("pipeline") or {})}
    pcfg = _build(PipelineConfig, p_kwargs, "pipeline")
    a_kwargs = dict(scn.get("admission") or {})
    acfg = _build(Admission, {"n_inflight": 4, "queue_cap": 40, **a_kwargs}, "admission")

    log = EventLog(paths.events)
    wall_start = datetime.now(timezone.utc)
    ledger = DailyLedger(gcfg.daily_token_cap, gcfg.daily_request_cap,
                         path=(cfg.paths.results_dir / f"ledger_{model.id.replace('/', '_')}.json") if real else None)
    gw = Gateway(client, gcfg, log, ledger=ledger, rng=random.Random(seed))
    pipe = Pipeline(retrievers, cfg.papers, gw, acfg, pcfg, log)

    # --- plan
    capacity = capacity_rps(used, float(scn.get("est_tokens_per_answer", 2200)))
    sequential = scn.get("kind") == "sequential"
    order = list(q_all)
    if not sequential:
        random.Random(seed).shuffle(order)
        if not scn.get("phases"):
            raise RelayError(f"scenario {name}: needs 'phases' (or kind: sequential)")
    if sequential:
        arrivals: list = []
        n_planned = min(int(scn.get("max_requests") or len(order)), len(order))
    else:
        arrivals = build_arrivals(list(scn["phases"]), capacity, seed)
        if real and len(arrivals) > int(scn["max_requests"]):
            arrivals = arrivals[: int(scn["max_requests"])]
        n_planned = len(arrivals)
    load_duration = (max((float(p["start_s"]) + float(p["duration_s"]) for p in scn.get("phases", [])), default=0.0)
                     if not sequential else None)

    config_hash = hashlib.sha256(json.dumps(
        {"scenario": dict(scn), "gateway": asdict(gcfg), "pipeline": asdict(pcfg)}, sort_keys=True, default=str
    ).encode()).hexdigest()
    source_label = "real" if real else "stand-in"
    manifest: dict[str, Any] = {
        "run_id": run_id, "scenario": name, "description": scn.get("description"), "source": source_label,
        "provenance": ("REAL service; limits DOCUMENTED (account console snapshot, config/limits.yaml)" if real else
                       f"SIMULATED stand-in; its limits are the DOCUMENTED limits x {scale:g} (a SIMULATED choice)"),
        "limits": {"documented": documented, "used": used, "scale": scale,
                   "used_provenance": "DOCUMENTED" if real and scale == 1.0 else "SIMULATED"},
        "capacity_rps": {"value": capacity, "kind": "arithmetic: min(RPM/60, TPM/60/est_tokens)",
                         "est_tokens_per_answer": scn.get("est_tokens_per_answer", 2200)},
        "standin_profile_at_start": standin_profile_before,
        "gateway_config": asdict(gcfg), "pipeline_config": asdict(pcfg),
        "admission": {"n_inflight": acfg.n, "queue_cap": acfg.queue_cap, "newest_first_after_s": acfg.newest_first_after_s,
                      "min_service_s": acfg.min_service_s, "estimate_check": acfg.estimate_check},
        "phases": scn.get("phases"), "controls": scn.get("controls"), "kind": scn.get("kind", "open_loop"),
        "seed": seed, "n_planned": n_planned, "load_duration_s": load_duration,
        "prices": {"in_per_mtok": model.price_in_per_mtok, "out_per_mtok": model.price_out_per_mtok,
                   "provenance": model.price_provenance, "note": "notional: the free tier costs nothing"},
        "model": model.id, "wall_start_utc": wall_start.isoformat(),
        "config_hash": config_hash, "scenarios_file": scenarios_file,
        "python": platform.python_version(), "questions_file": str(cfg.paths.questions), "n_questions": len(q_all),
        "cache_enabled": pcfg.cache_enabled,
        "note": "No quality number may be taken from a stand-in run: its replies are templated.",
    }
    write_manifest(paths.manifest, manifest)
    log.emit(None, "runner", "run_start", run_id=run_id, scenario=name, source=source_label, n_planned=n_planned)

    clock = time.monotonic
    tasks: list = []
    sampler = asyncio.ensure_future(_sampler(log, gw, acfg, float(scn.get("state_interval_s", 1.0))))
    control_tasks: list = []
    t_start = clock()

    def make_ctx(i: int, q: Any) -> RequestContext:
        now = clock()
        return RequestContext(request_id=f"{run_id}-{i:04d}", scenario=name, question_id=q.id, question=q.text,
                              arrive=now, deadline=now + deadline_s, log=log, clock=clock, setting=setting)

    try:
        if not real:
            for c in scn.get("controls") or []:
                body = {"set": resolve_control(c.get("set") or {}, used), "reset": bool(c.get("reset", False))}
                control_tasks.append(asyncio.ensure_future(
                    _control(http, standin_url, float(c["at_s"]), body, t_start, log)))   # type: ignore[arg-type]
        if sequential:
            for i, q in enumerate(order[:n_planned]):
                ctx = make_ctx(i, q)
                log.emit(ctx.request_id, "launch", "launch", lag_ms=0.0)
                await pipe.handle(ctx, q.text)
        else:
            for i, at in enumerate(arrivals):
                delay = t_start + at - clock()
                if delay > 0:
                    await asyncio.sleep(delay)
                q = order[i % len(order)]
                ctx = make_ctx(i, q)
                log.emit(ctx.request_id, "launch", "launch", lag_ms=round(max(0.0, -delay) * 1000.0, 2))
                tasks.append(asyncio.ensure_future(pipe.handle(ctx, q.text)))
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True),
                                       timeout=deadline_s + float(scn.get("drain_s", 10)))
    except asyncio.TimeoutError:
        log.emit(None, "runner", "drain_timeout", pending=sum(1 for t in tasks if not t.done()))
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        sampler.cancel()
        for t in control_tasks:
            t.cancel()
        await asyncio.gather(sampler, *control_tasks, return_exceptions=True)
        standin_stats = None
        if not real:
            try:
                standin_stats = (await http.get(f"{standin_url}/stats")).json()
            except (httpx.HTTPError, ValueError):
                standin_stats = None
        log.emit(None, "runner", "run_end", run_id=run_id)
        wall_end = datetime.now(timezone.utc)
        log.close()
        await http.aclose()

    # --- artifacts
    events = read_events(paths.events)
    manifest_now = {**manifest, "wall_end_utc": wall_end.isoformat(), "standin_stats_at_end": standin_stats}
    rows = request_rows(events, manifest_now)
    write_results_csv(paths.results, csv_rows(rows))
    inv_params = {
        "max_attempts": gcfg.max_attempts_per_request, "n_inflight": acfg.n, "reserve_s": gcfg.reserve_s,
        "discretionary_share": gcfg.discretionary_share if gcfg.limiter_enabled else None,
        "global_pause": bool(gcfg.global_pause and gcfg.limiter_enabled),
    }
    invariants = check_invariants(events, inv_params)
    summary = summarize(rows, events, manifest_now)
    lags = [e["lag_ms"] for e in events if e["event"] == "launch"]
    summary["max_launch_lag_ms"] = max(lags) if lags else None
    write_manifest(paths.manifest, {**manifest_now, "invariant_params": inv_params, "invariants": invariants,
                                    "summary": summary})
    return RunResult(run_id=run_id, paths=paths, summary=summary, invariants=invariants, n_rows=len(rows))
