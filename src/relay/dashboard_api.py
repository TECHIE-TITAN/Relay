"""Dashboard API (DESIGN.md section 14): read-only history, plus one opt-in write.

* History is GET only: no route changes any run. Nothing here imports the LLM client or reads an API key, so the
  dashboard cannot reach the provider.
* The one write, enabled only with enable_simulations=True (the CLI default; `--read-only` turns it off), is starting a
  STAND-IN simulation: POST /api/simulations launches the existing runner as a subprocess through relay.simulations
  (stand-in only, no key in its environment, runs named sim_*). Stopping one is POST /api/simulations/{id}/stop.
* Every number is read from files already on disk (run manifests, events.jsonl, results.csv, probe_real.jsonl, the ledger).
  Nothing is computed from a guess; a missing file gives `available: false`, never a made-up value.
* Provenance labels are carried through: DOCUMENTED, OBSERVED-REPORTED, OBSERVED-TRIGGERED, SIMULATED.
* `GET /api/runs/{id}/stream` is Server-Sent Events: it replays events.jsonl and, while the run is still going (its manifest
  has no wall_end_utc yet), keeps following the file.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

import csv

import yaml

from .analyze import _retrieval_scores, scenario_stats
from .artifacts import list_runs, read_events, read_manifest, read_results_csv, run_paths
from .metrics import percentile, request_rows
from .schema import RelayError
from .simulations import SIM_PREFIX, SimManager, SimRequest
from .standin.profile import StandinProfile

LIVE_STALE_S = 30.0       # a run with no end time and no new event for this long is "interrupted", not live
ROW_FIELDS = ("start_time", "first_token_ms", "total_ms", "status", "retries", "tokens_in", "tokens_out", "retrieval_setting", "answer_grade")

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,160}$")
_CHUNK_ID_RE = re.compile(r"^[A-Za-z0-9_-]+:(S\d+):\d{1,6}$")
_ORG_RE = re.compile(r"org_[A-Za-z0-9]{10,}")
MANIFEST_KEYS = ("run_id", "scenario", "description", "source", "provenance", "kind", "wall_start_utc", "wall_end_utc",
                 "limits", "capacity_rps", "gateway_config", "pipeline_config", "admission", "phases", "controls", "seed",
                 "n_planned", "prices", "model", "config_hash", "summary", "invariants", "incomplete", "stop", "grading", "note")


def _clean(s: Any) -> Any:
    return _ORG_RE.sub("org_[REDACTED]", s) if isinstance(s, str) else s


def _live_state(paths, manifest: Mapping[str, Any]) -> tuple:
    """(live, interrupted): a run with no end time is live while its event log still grows."""
    if manifest.get("wall_end_utc") or not manifest.get("wall_start_utc"):
        return False, False
    try:
        age = time.time() - paths.events.stat().st_mtime
    except OSError:
        age = 0.0
    return age <= LIVE_STALE_S, age > LIVE_STALE_S


def create_app(results_dir: Path, *, root: Path, documented_limits: Mapping[str, Any], prices: Mapping[str, Any],
               allow_origins: tuple = ("http://localhost:5173", "http://127.0.0.1:5173"),
               enable_simulations: bool = False, sim_manager: Optional[SimManager] = None) -> FastAPI:
    app = FastAPI(title="Relay dashboard API")
    app.add_middleware(CORSMiddleware, allow_origins=list(allow_origins),
                       allow_methods=["GET", "POST"] if enable_simulations else ["GET"], allow_headers=["*"])
    sims = (sim_manager or SimManager(results_dir, root, dict(documented_limits))) if enable_simulations else None

    def paths_for(rid: str):
        if not _ID_RE.match(rid):
            raise HTTPException(400, "invalid run id")
        try:
            p = run_paths(results_dir, rid)
        except RelayError:
            raise HTTPException(400, "invalid run id")
        if not p.manifest.is_file():
            raise HTTPException(404, f"run '{rid}' not found")
        return p

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "read_only": sims is None, "simulations_enabled": sims is not None, "runs": len(list_runs(results_dir))}

    @app.get("/api/runs")
    def runs() -> list:
        out = []
        for rid in reversed(list_runs(results_dir)):                       # newest first
            p = run_paths(results_dir, rid)
            m = read_manifest(p.manifest)
            n = len(read_results_csv(p.results)) if p.results.is_file() else None
            live, interrupted = _live_state(p, m)
            out.append({"run_id": rid, "scenario": m.get("scenario"), "source": m.get("source"), "kind": m.get("kind"),
                        "wall_start_utc": m.get("wall_start_utc"), "wall_end_utc": m.get("wall_end_utc"), "rows": n,
                        "live": live, "interrupted": interrupted,
                        "origin": "simulation" if str(m.get("scenario", "")).startswith(SIM_PREFIX) else "evidence",
                        "incomplete": bool(m.get("incomplete")), "graded": bool(m.get("grading")),
                        "provenance": m.get("provenance")})
        return out

    @app.get("/api/rows")
    def rows(source: str = "all", origin: str = "evidence", scope: str = "latest") -> dict:
        """Request rows across runs for the Overview. source: real | stand-in | all. origin: evidence | simulation | all
        (simulations started from the dashboard are kept out of the evidence by default). scope: latest run per scenario | all runs."""
        if source not in ("all", "real", "stand-in") or origin not in ("all", "evidence", "simulation") or scope not in ("latest", "all"):
            raise HTTPException(400, "source is all|real|stand-in, origin is all|evidence|simulation, scope is latest|all")
        picked: dict = {}
        for rid in list_runs(results_dir):
            p = run_paths(results_dir, rid)
            m = read_manifest(p.manifest)
            sc = str(m.get("scenario", ""))
            is_sim = sc.startswith(SIM_PREFIX)
            if (origin == "evidence" and is_sim) or (origin == "simulation" and not is_sim) or not p.results.is_file():
                continue
            if source != "all" and m.get("source") != source:
                continue
            key = sc if scope == "latest" else rid
            if key not in picked or str(m.get("wall_start_utc")) >= picked[key][1]:
                picked[key] = (rid, str(m.get("wall_start_utc")), m)
        out = []
        meta: dict = {}
        for key, (rid, _, m) in picked.items():
            summ = m.get("summary") or {}
            meta[rid] = {"scenario": m.get("scenario"), "source": m.get("source"), "kind": m.get("kind"),
                         "wall_start_utc": m.get("wall_start_utc"), "peak_queue_len": summ.get("peak_queue_len"),
                         "peak_inflight": summ.get("peak_inflight"), "incomplete": bool(m.get("incomplete"))}
            for r in read_results_csv(run_paths(results_dir, rid).results):
                out.append({"run_id": rid, "scenario": m.get("scenario"), "source": m.get("source"), **{k: r.get(k) for k in ROW_FIELDS}})
        return {"count": len(out), "runs": [v[0] for v in picked.values()], "run_meta": meta, "rows": out}

    @app.get("/api/runs/{rid}/events")
    def run_events(rid: str, names: str = "", limit: int = 500) -> list:
        """Events of this run that belong to no request (limit changes, pauses, breaker, controls ...), optionally by name."""
        p = paths_for(rid)
        wanted = {n for n in names.split(",") if n}
        if not p.events.is_file():
            return []
        out = [e for e in read_events(p.events) if e.get("request_id") is None and e.get("event") != "gateway_state"
               and (not wanted or e.get("event") in wanted)]
        return out[: max(1, min(limit, 5000))]

    @app.get("/api/runs/{rid}")
    def run(rid: str) -> dict:
        m = read_manifest(paths_for(rid).manifest)
        return {k: m[k] for k in MANIFEST_KEYS if k in m}

    @app.get("/api/runs/{rid}/requests")
    def requests(rid: str) -> list:
        p = paths_for(rid)
        if not p.events.is_file():
            return []
        return request_rows(read_events(p.events), read_manifest(p.manifest))

    @app.get("/api/runs/{rid}/requests/{req}")
    def trace(rid: str, req: str) -> dict:
        if not _ID_RE.match(req):
            raise HTTPException(400, "invalid request id")
        p = paths_for(rid)
        events = read_events(p.events) if p.events.is_file() else []
        mine = [e for e in events if e.get("request_id") == req]
        if not mine:
            raise HTTPException(404, f"request '{req}' not found in run '{rid}'")
        row = next((r for r in request_rows(events, read_manifest(p.manifest)) if r["request_id"] == req), None)
        return {"request_id": req, "row": row, "events": mine}

    @app.get("/api/runs/{rid}/state")
    def state(rid: str) -> list:
        p = paths_for(rid)
        if not p.events.is_file():
            return []
        return [{k: v for k, v in e.items() if k not in ("request_id", "stage")}
                for e in read_events(p.events) if e.get("event") == "gateway_state"]

    @app.get("/api/runs/{rid}/stream")
    async def stream(rid: str, poll_s: float = 0.5) -> StreamingResponse:
        p = paths_for(rid)

        async def gen():
            sent = 0
            while True:
                text = p.events.read_text(encoding="utf-8") if p.events.is_file() else ""
                lines = text.split("\n")[:-1]           # drops the empty tail after the last newline, or a line still being written
                for line in lines[sent:]:
                    if line.strip():
                        yield f"data: {line}\n\n"
                sent = len(lines)
                m = read_manifest(p.manifest)
                if m.get("wall_end_utc"):               # the run is finished: nothing more will come
                    yield "event: end\ndata: {}\n\n"
                    return
                if _live_state(p, m)[1]:                # no end time and the log stopped growing: the run was interrupted
                    yield "event: end\ndata: {\"interrupted\": true}\n\n"
                    return
                await asyncio.sleep(max(0.05, poll_s))

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/experiments")
    def experiments() -> dict:
        csv_path = root / "results.csv"
        if not csv_path.is_file():
            return {"available": False, "reason": "results.csv has not been produced yet (run `analyze`)"}
        rows = read_results_csv(csv_path)
        by: dict = {}
        for r in rows:
            by.setdefault(r["scenario"], []).append(r)
        src_path = root / "results.sources.json"
        sources = json.loads(src_path.read_text(encoding="utf-8")) if src_path.is_file() else None
        model_ttft: dict = {}          # the model's own time to first token per call: from the events, not a results.csv column
        grade_detail: dict = {}        # how many graded answers came from the model, from a quoted sentence, or were refusals
        for r in (sources or {}).get("runs_used", []):
            sc, rid = str(r.get("scenario", "")), str(r.get("run_id", ""))
            if not sc.startswith("quality_") or not _ID_RE.match(rid):
                continue
            p = run_paths(results_dir, rid)
            if p.events.is_file():
                vals = [float(e["ttft_ms"]) for e in read_events(p.events) if e.get("event") == "model_first_token" and e.get("ttft_ms") is not None]
                model_ttft[sc] = {"n": len(vals), "p50": percentile(vals, 50), "p95": percentile(vals, 95)}
            g = p.root / "grades.csv"
            if g.is_file():
                with g.open(newline="", encoding="utf-8") as f:
                    counts: dict = {}
                    for gr in csv.DictReader(f):
                        if (gr.get("detail_grade") or "").strip():
                            k = f"{gr.get('answer_mode')}:{gr['detail_grade'].strip()}"
                            counts[k] = counts.get(k, 0) + 1
                grade_detail[sc] = counts
        retrieval = None
        ev_files = sorted((results_dir / "retrieval_eval").glob("retrieval_eval_*.json")) if (results_dir / "retrieval_eval").is_dir() else []
        if ev_files:
            retrieval = {"file": ev_files[-1].name, "settings": _retrieval_scores(json.loads(ev_files[-1].read_text(encoding="utf-8")))}
        settings: dict = {}
        cfg_path = root / "config" / "relay.yaml"
        if cfg_path.is_file():
            try:
                settings = ((yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}).get("retrieval") or {}).get("settings") or {}
            except yaml.YAMLError:
                settings = {}
        return {"available": True, "rows": len(rows), "scenarios": {sc: scenario_stats(rs, prices) for sc, rs in by.items()},
                "sources": sources, "model_ttft": model_ttft, "grade_detail": grade_detail, "retrieval": retrieval, "settings": settings}

    @app.get("/api/chunks")
    def chunks(ids: str) -> dict:
        """The retrieved passages named by a trace (ids like attention:S2:0006), read from data/processed."""
        wanted = [i for i in ids.split(",") if i][:20]
        by_setting: dict = {}
        for i in wanted:
            m = _CHUNK_ID_RE.match(i)
            if not m:
                raise HTTPException(400, f"invalid chunk id {i!r}")
            by_setting.setdefault(m.group(1), set()).add(i)
        found: dict = {}
        for setting, idset in by_setting.items():
            f = root / "data" / "processed" / f"chunks_{setting}.jsonl"
            if not f.is_file():
                continue
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                if d.get("chunk_id") in idset:
                    found[d["chunk_id"]] = {"chunk_id": d["chunk_id"], "paper": d.get("paper_key"), "setting": d.get("setting_id"),
                                            "pdf_pages": [d.get("page_start"), d.get("page_end")], "printed_pages": [d.get("printed_start"), d.get("printed_end")],
                                            "section": d.get("section"), "approx_tokens": d.get("approx_tokens"),
                                            "text": str(d.get("text", ""))[:900], "clipped": len(str(d.get("text", ""))) > 900}
        return {"chunks": [found.get(i) or {"chunk_id": i, "missing": True} for i in wanted]}

    @app.get("/api/limits")
    def limits() -> dict:
        probe = results_dir / "probe_real.jsonl"
        raw = []
        if probe.is_file():
            raw = [json.loads(line) for line in probe.read_text(encoding="utf-8").splitlines() if line.strip()]
        real = [d for d in raw if d.get("source") == "real"]
        stages: dict = {}
        for d in real:
            stages[d["stage"]] = stages.get(d["stage"], 0) + 1
        hdr = next((d["headers"] for d in reversed(real) if d.get("headers") and "x-ratelimit-limit-tokens" in d["headers"]), None)
        first429 = next((d for d in real if d.get("error_kind") == "rate_limit_minute"), None)
        sim = StandinProfile().to_dict()
        return {
            "documented": {"provenance": "DOCUMENTED (account console snapshot, config/limits.yaml)", **dict(documented_limits)},
            "observed_reported": ({"provenance": "OBSERVED-REPORTED (response headers of the real probe)",
                                   "limit_requests_per_day": hdr.get("x-ratelimit-limit-requests"),
                                   "limit_tokens_per_minute": hdr.get("x-ratelimit-limit-tokens")} if hdr else None),
            "observed_triggered": ({"provenance": "OBSERVED-TRIGGERED (the real service returned this when the limit was hit)",
                                    "error_kind": first429.get("error_kind"), "status_code": first429.get("status_code"),
                                    "message": _clean(first429.get("message")), "retry_after_s": first429.get("retry_after_s"),
                                    "headers": first429.get("headers")} if first429 else None),
            "probe_calls_by_stage": stages,
            "simulated": {"provenance": "SIMULATED (stand-in defaults; scenarios scale the limits by limits_scale)",
                          **{k: sim[k] for k in ("rpm", "tpm", "rpd", "tpd", "ttft_median_ms", "tokens_per_s",
                                                 "p_5xx", "p_hang", "p_slow", "p_midstream")}},
            "daily_limit_note": "The per-day behaviour is simulated; it was never triggered on the real service.",
        }

    @app.get("/api/ledger")
    def ledger() -> dict:
        files = sorted(results_dir.glob("ledger_*.json"))
        if not files:
            return {"available": False}
        d = json.loads(files[-1].read_text(encoding="utf-8"))
        return {"available": True, "file": files[-1].name, **d}

    if sims is not None:
        @app.get("/api/simulations/options")
        def sim_options() -> dict:
            return sims.options()

        @app.get("/api/simulations")
        def sim_list() -> list:
            return sims.list()

        @app.post("/api/simulations", status_code=201)
        def sim_start(req: SimRequest) -> dict:
            try:
                return sims.start(req)
            except ValueError as e:                       # a combination the runner cannot express (for example a too-short burst)
                raise HTTPException(422, str(e))
            except RuntimeError as e:                     # too many running
                raise HTTPException(409, str(e))

        @app.post("/api/simulations/{sim_id}/stop")
        def sim_stop(sim_id: str) -> dict:
            if not _ID_RE.match(sim_id):
                raise HTTPException(400, "invalid simulation id")
            try:
                return sims.stop(sim_id)
            except KeyError:
                raise HTTPException(404, f"simulation '{sim_id}' not found")

        @app.on_event("shutdown")
        def _stop_simulations() -> None:
            sims.shutdown()                               # never leave a runner or stand-in process behind

    return app
