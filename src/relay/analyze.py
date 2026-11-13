"""Merge the runs into the submission `results.csv` and compute the brief's tables FROM THAT FILE.

* `results.csv` = the chosen run of each scenario (the latest by start time; pilots excluded) plus the real probe
  calls (`results/probe_real.jsonl`, scenario `probe_<stage>`). Older runs of a scenario stay on disk as raw results.
* Every table figure is computed from the merged `results.csv` read back from disk, except two that the file cannot
  hold and that are marked: peak queue length (from the run's manifest) and the per-setting retrieval-only scores
  (from `results/retrieval_eval`). Nothing is invented: a figure that was not measured says "not measured".
* Latency percentiles use requests with status ok (the type-7 definition in metrics.percentile) and say how many.
  p99 is not reported below 100 requests.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .artifacts import list_runs, read_events, read_manifest, read_results_csv, run_paths, write_results_csv
from .metrics import percentile, request_rows

EXCLUDED_SCENARIOS = frozenset({"quality_measure"})          # the three pilot runs
STANDIN_ORDER = ("smoke", "steady", "burst", "sustained_overload", "limit_halved", "limit_halved_hot",
                 "daily_exhausted", "retry_naive", "retry_budgeted", "retry_naive_hot", "retry_budgeted_hot")
REAL_ORDER = ("quality_S1", "quality_S2", "quality_S3", "quality_S4", "probe_single", "probe_latency",
              "probe_concurrency", "probe_limit")
NM = "not measured"


# ------------------------------------------------------------------------------------------ building the file

def _probe_status(d: Mapping[str, Any]) -> str:
    if d.get("ok"):
        return "ok"
    kind = d.get("error_kind") or ""
    if kind.startswith("rate_limit"):
        return "rate_limited"
    if kind in ("timeout", "first_token_timeout"):
        return "timeout"
    return "error"


def probe_rows(path: Path) -> list:
    """The real probe calls as results.csv rows (scenario probe_<stage>, source real, no retrieval, no grade)."""
    rows = []
    if not path.is_file():
        return rows
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("source") != "real":
            continue
        ttft = d.get("ttft_ms")
        rows.append({
            "request_id": f"probe-{d['stage']}-{i:03d}", "scenario": f"probe_{d['stage']}", "source": "real",
            "start_time": d["ts"], "first_token_ms": "" if ttft is None else round(float(ttft), 1),
            "total_ms": round(float(d.get("total_ms") or 0.0), 1), "status": _probe_status(d), "retries": 0,
            "tokens_in": int(d.get("tokens_in") or 0), "tokens_out": int(d.get("tokens_out") or 0),
            "retrieval_setting": "", "answer_grade": "",
        })
    return rows


def select_runs(results_dir: Path, exclude: frozenset = EXCLUDED_SCENARIOS) -> dict:
    """scenario -> (run_id, manifest): the latest run of each scenario by wall start time."""
    found: dict = {}
    for rid in list_runs(results_dir):
        paths = run_paths(results_dir, rid)
        if not paths.results.is_file():
            continue
        m = read_manifest(paths.manifest)
        sc = m.get("scenario")
        if sc in exclude or not m.get("wall_start_utc"):
            continue
        if sc not in found or m["wall_start_utc"] >= found[sc][1]["wall_start_utc"]:
            found[sc] = (rid, m)
    return found


# ------------------------------------------------------------------------------------------ computing from the file

def _f(x: Any) -> Optional[float]:
    if x is None or x == "":
        return None
    return float(x)


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _pct(vals: Sequence[float], q: float, min_n: int = 1) -> Optional[float]:
    return percentile(vals, q) if len(vals) >= min_n else None


def latency_block(rows: Sequence[Mapping[str, Any]]) -> dict:
    ok = [r for r in rows if r["status"] == "ok"]
    ft = [v for v in (_f(r["first_token_ms"]) for r in ok) if v is not None]
    tt = [v for v in (_f(r["total_ms"]) for r in ok) if v is not None]
    return {"n_ok": len(ok),
            "ft": {"n": len(ft), "p50": _pct(ft, 50), "p95": _pct(ft, 95), "p99": _pct(ft, 99, 100)},
            "total": {"n": len(tt), "p50": _pct(tt, 50), "p95": _pct(tt, 95), "p99": _pct(tt, 99, 100)}}


def scenario_stats(rows: Sequence[Mapping[str, Any]], prices: Mapping[str, float]) -> dict:
    n = len(rows)
    starts = sorted(_t(r["start_time"]) for r in rows)
    ends = [_t(r["start_time"]).timestamp() + (_f(r["total_ms"]) or 0.0) / 1000.0 for r in rows]
    duration = (max(ends) - starts[0].timestamp()) if rows else None
    span = (starts[-1] - starts[0]).total_seconds() if n >= 2 else 0.0
    by = {s: sum(1 for r in rows if r["status"] == s) for s in ("ok", "rate_limited", "timeout", "error")}
    ok = [r for r in rows if r["status"] == "ok"]
    t_in = sum(int(r["tokens_in"] or 0) for r in ok)
    t_out = sum(int(r["tokens_out"] or 0) for r in ok)
    model_ok = [r for r in ok if int(r["tokens_in"] or 0) + int(r["tokens_out"] or 0) > 0]
    graded = [r for r in rows if r.get("answer_grade")]
    grades = {g: sum(1 for r in graded if r["answer_grade"] == g)
              for g in ("correct", "wrong", "refused_correctly", "refused_wrongly")}
    cost = None
    if ok and prices.get("in") is not None and prices.get("out") is not None:
        cost = (t_in * prices["in"] + t_out * prices["out"]) / 1e6 / len(ok) * 1000.0
    return {
        "n": n, "duration_s": duration, "offered_per_min": (n / span * 60.0) if span > 0 else None, "by_status": by,
        "retries_per_request": (sum(int(r["retries"] or 0) for r in rows) / n) if n else None,
        "tokens_per_answer": ((t_in + t_out) / len(ok)) if ok else None,
        "tokens_per_model_answer": ((sum(int(r["tokens_in"] or 0) + int(r["tokens_out"] or 0) for r in model_ok)
                                     / len(model_ok)) if model_ok else None),
        "n_model_answers": len(model_ok), "cost_per_1000": cost, "n_graded": len(graded), "grades": grades,
        "latency": latency_block(rows),
    }


def slowest_request(events: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any]) -> Optional[dict]:
    """The slowest successful request of a run, split into waiting / working / retrying, with its event timeline."""
    rows = [r for r in request_rows(events, manifest) if r["status"] == "ok" and r["first_token_ms"] is not None]
    if not rows:
        return None
    r = max(rows, key=lambda x: x["total_ms"])
    evs = [e for e in events if e.get("request_id") == r["request_id"]]
    t0 = next((e["t_mono"] for e in evs if e["event"] == "arrive"), evs[0]["t_mono"])
    keep = {"arrive", "slot_acquired", "limiter_wait_end", "send", "call_end", "retry_sleep_start", "pause_start",
            "verify_result", "degrade", "loop_regenerate", "first_token", "done"}
    timeline = []
    for e in evs:
        if e["event"] not in keep:
            continue
        detail = {k: e[k] for k in ("wait_ms", "waited_ms", "purpose", "ok", "error_kind", "duration_ms", "sleep_ms",
                                    "passed", "mode", "status", "attempt", "reason") if k in e}
        timeline.append({"t_ms": round((e["t_mono"] - t0) * 1000.0, 1), "event": e["event"], **detail})
    return {"request_id": r["request_id"], "question_id": r.get("question_id"), "total_ms": r["total_ms"],
            "first_token_ms": r["first_token_ms"], "after_first_token_ms": round(r["total_ms"] - r["first_token_ms"], 1),
            "waiting_ms": r["waiting_ms"], "working_ms": r["working_ms"], "retrying_ms": r["retrying_ms"],
            "unaccounted_ms": r["unaccounted_ms"], "retries": r["retries"], "timeline": timeline}


# ------------------------------------------------------------------------------------------ orchestration

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_sources(results_dir: Path, out_csv: Path, chosen: Mapping[str, Any], n_probe: int,
                  probe_path: Optional[Path], n_rows: int) -> Path:
    """results.sources.json: which run produced which rows of results.csv, with checksums, and which runs were left out and why.

    Every request_id in results.csv starts with its run id (probe calls start with `probe-`), so any row can be traced
    to a folder under results/runs. analyze only READS the run folders; it never writes into them."""
    used = {rid for rid, _m in chosen.values()}
    runs, left_out = [], []
    for sc, (rid, m) in sorted(chosen.items()):
        paths = run_paths(results_dir, rid)
        runs.append({"scenario": sc, "run_id": rid, "run_dir": f"results/runs/{rid}", "rows": len(read_results_csv(paths.results)),
                     "results_csv_sha256": _sha256(paths.results), "manifest_sha256": _sha256(paths.manifest),
                     "events_sha256": _sha256(paths.events) if paths.events.is_file() else None,
                     "wall_start_utc": m.get("wall_start_utc"), "config_hash": m.get("config_hash"),
                     "incomplete": bool(m.get("incomplete")), "graded": bool(m.get("grading"))})
    for rid in list_runs(results_dir):
        if rid in used:
            continue
        m = read_manifest(run_paths(results_dir, rid).manifest)
        sc = m.get("scenario")
        why = ("pilot run, kept as evidence and never merged" if sc in EXCLUDED_SCENARIOS
               else f"superseded by a later run of scenario '{sc}'" if sc in chosen else "no results.csv")
        left_out.append({"run_id": rid, "scenario": sc, "reason": why})
    doc = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "results_csv": out_csv.name,
           "results_csv_sha256": _sha256(out_csv), "rows": n_rows,
           "rows_from_runs": sum(r["rows"] for r in runs), "rows_from_probe": n_probe,
           "probe_file": probe_path.name if probe_path and probe_path.is_file() else None,
           "probe_sha256": _sha256(probe_path) if probe_path and probe_path.is_file() else None,
           "runs_used": runs, "runs_left_out": left_out,
           "note": "request_id starts with the run id; probe calls start with 'probe-'. Run folders are never modified by analyze."}
    path = out_csv.with_name("results.sources.json")
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def analyze(results_dir: Path, out_csv: Path, out_md: Path, *, probe_path: Optional[Path], prices: Mapping[str, float],
            settings_desc: Mapping[str, str], eval_dir: Optional[Path] = None) -> dict:
    chosen = select_runs(results_dir)
    rows: list = []
    for sc, (rid, _m) in chosen.items():
        rows.extend(read_results_csv(run_paths(results_dir, rid).results))
    probes = probe_rows(probe_path) if probe_path else []
    rows.extend(probes)
    write_results_csv(out_csv, rows)
    sources = write_sources(results_dir, out_csv, chosen, len(probes), probe_path, len(rows))

    merged = read_results_csv(out_csv)                       # every table below is computed from the file on disk
    by_sc: dict = {}
    for r in merged:
        by_sc.setdefault(r["scenario"], []).append(r)
    stats = {sc: scenario_stats(rs, prices) for sc, rs in by_sc.items()}

    warnings = []
    for sc, (rid, m) in chosen.items():
        if m.get("incomplete"):
            warnings.append(f"{sc} ({rid}) is INCOMPLETE: {(m.get('stop') or {}).get('reason')}; "
                            f"{len((m.get('stop') or {}).get('not_run') or [])} questions were not run")
        if sc.startswith("quality_") and not m.get("grading"):
            warnings.append(f"{sc} ({rid}) has not been graded (apply-grades): answer_grade is empty")

    model_ttft: dict = {}          # per scenario: the model's own time to first token, from events (not a results.csv column)
    for sc, (rid, m) in chosen.items():
        evs = read_events(run_paths(results_dir, rid).events) if run_paths(results_dir, rid).events.is_file() else []
        vals = [float(e["ttft_ms"]) for e in evs if e.get("event") == "model_first_token" and e.get("ttft_ms") is not None]
        model_ttft[sc] = {"n": len(vals), "p50": _pct(vals, 50), "p95": _pct(vals, 95)}

    slow = None
    for sc, (rid, m) in chosen.items():
        if m.get("source") != "stand-in":
            continue
        cand = slowest_request(read_events(run_paths(results_dir, rid).events), m)
        if cand and (slow is None or cand["total_ms"] > slow["total_ms"]):
            slow = {**cand, "scenario": sc, "run_id": rid}

    retrieval = None
    if eval_dir and eval_dir.is_dir():
        files = sorted(eval_dir.glob("retrieval_eval_*.json"))
        if files:
            retrieval = {"file": files[-1].name, "settings": _retrieval_scores(json.loads(files[-1].read_text(encoding="utf-8")))}

    result = {"scenarios": stats, "runs": {sc: rid for sc, (rid, _m) in chosen.items()}, "sources_file": str(sources),
              "meta": {sc: {"kind": m.get("kind"), "source": m.get("source"),
                            "peak_queue_len": (m.get("summary") or {}).get("peak_queue_len"),
                            "incomplete": bool(m.get("incomplete"))} for sc, (_r, m) in chosen.items()},
              "n_rows": len(merged), "warnings": warnings, "slowest": slow, "retrieval": retrieval, "model_ttft": model_ttft,
              "settings_desc": dict(settings_desc), "prices": dict(prices)}
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(render_markdown(result), encoding="utf-8")
    return result


def _retrieval_scores(doc: Mapping[str, Any]) -> dict:
    out: dict = {}
    for r in doc.get("per_question", []):
        s = out.setdefault(r["setting"], {"asked": 0, "answerable": 0, "answerable_abstained": 0, "unanswerable_reach_model": 0,
                                          "recall_sum": 0.0, "hit_all": 0, "ctx": 0, "reach": 0})
        s["asked"] += 1
        if r["answerable"]:
            s["answerable"] += 1
            s["recall_sum"] += float(r.get("evidence_recall") or 0.0)
            s["answerable_abstained"] += 1 if r["abstain"] else 0
            s["hit_all"] += 1 if r.get("hit_all") else 0
        elif not r["abstain"]:
            s["unanswerable_reach_model"] += 1
        if not r["abstain"]:
            s["reach"] += 1
            s["ctx"] += int(r["context_tokens_approx"])
    return out


# ------------------------------------------------------------------------------------------ markdown

def _ms(v: Optional[float]) -> str:
    return NM if v is None else f"{v:.1f}"


def _trio(b: Mapping[str, Any]) -> str:
    p99 = b["p99"]
    return f"{_ms(b['p50'])} / {_ms(b['p95'])} / " + (f"{p99:.1f}" if p99 is not None else "n<100")


def _pair(b: Mapping[str, Any]) -> str:
    return f"{_ms(b['p50'])} / {_ms(b['p95'])}"


def _pc(count: int, n: int) -> str:
    return f"{count / n * 100:.1f}" if n else NM


def _order(names: Sequence[str]) -> list:
    known = list(STANDIN_ORDER) + list(REAL_ORDER)
    return [n for n in known if n in names] + sorted(n for n in names if n not in known)


def render_markdown(res: Mapping[str, Any]) -> str:
    st, meta = res["scenarios"], res["meta"]
    names = _order(list(st))
    src = lambda sc: ("real" if (meta.get(sc, {}).get("source") == "real" or sc.startswith("probe_")) else "stand-in")   # noqa: E731
    closed = lambda sc: sc.startswith("probe_") or meta.get(sc, {}).get("kind") == "sequential"                          # noqa: E731
    L = ["# Tables (generated by `analyze` from results.csv; do not edit by hand)", ""]
    if res["warnings"]:
        L += ["**Warnings**", ""] + [f"- {w}" for w in res["warnings"]] + [""]
    L += ["## Table 2a: Latency, one row per scenario", "",
          "Percentiles over requests with status ok (type 7); `n ok` says how many. p99 is shown only with at least 100 requests. "
          "Offered rate = requests / span of start times; for sequential real runs and probes it is a closed loop, not an offered load.", "",
          "| Scenario | Real or stand-in | Requests sent | Duration (s) | Offered rate (per min) | n ok | First token p50 / p95 / p99 (ms) | Total time p50 / p95 / p99 (ms) |",
          "|---|---|---|---|---|---|---|---|"]
    for sc in names:
        s = st[sc]
        rate = NM if s["offered_per_min"] is None else f"{s['offered_per_min']:.1f}" + (" (closed loop)" if closed(sc) else "")
        L.append(f"| {sc} | {src(sc)} | {s['n']} | {_ms(s['duration_s'])} | {rate} | {s['latency']['n_ok']} | "
                 f"{_trio(s['latency']['ft'])} | {_trio(s['latency']['total'])} |")
    L += ["", "## Table 2b: Outcomes, same rows", "",
          "Tokens per answer = provider-reported tokens of answers released, divided by answers released (free refusals count as 0). "
          "Cost is NOTIONAL (the free tier bills nothing): tokens x the configured price per million. "
          "Peak queue length is not a results.csv column; it comes from the run's manifest. Correctly refused % is over graded requests.", "",
          "| Scenario | Succeeded % | Rate-limited % | Timed out or given up % | Error % | Retries per request | Peak queue length | Tokens per answer | Cost per 1,000 answers (USD, notional) | Correctly refused % |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for sc in names:
        s = st[sc]
        n, b = s["n"], s["by_status"]
        pq = meta.get(sc, {}).get("peak_queue_len")
        refused = _pc(s["grades"]["refused_correctly"], s["n_graded"]) if s["n_graded"] else NM
        L.append(f"| {sc} | {_pc(b['ok'], n)} | {_pc(b['rate_limited'], n)} | {_pc(b['timeout'], n)} | {_pc(b['error'], n)} | "
                 f"{'' if s['retries_per_request'] is None else format(s['retries_per_request'], '.2f')} | "
                 f"{NM if pq is None else pq} | {_ms(s['tokens_per_answer'])} | "
                 f"{NM if s['cost_per_1000'] is None else format(s['cost_per_1000'], '.4f')} | {refused} |")
    L += ["", "## Table 3: Retrieval settings (real model, same 20 questions)", "",
          "Quality is the author's manual grade of every answer released (extractive answers included; see grades.csv for the split by "
          "answer mode). Percentages are of questions asked. Tokens per answer: all answers / model answers only. "
          "First token and total time are over status ok requests and include any wait for the token limit (the questions are sent "
          "back to back). Model-call TTFT is the model's own time to first token for each call, from the events (not a results.csv column).", "",
          "| Setting (piece size, pieces fetched, re-rank) | Questions asked | Correct and cited % | Wrong % | Refused correctly % | Refused wrongly % | Tokens per answer (all / model only) | Model-call TTFT p50 / p95 (ms) | First token p50 / p95 (ms) | Total time p50 / p95 (ms) |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for sc in [n for n in names if n.startswith("quality_")]:
        s, key = st[sc], sc.split("_", 1)[1]
        g = s["grades"]
        desc = res["settings_desc"].get(key, key)
        q = (_pc(g["correct"], s["n"]), _pc(g["wrong"], s["n"]), _pc(g["refused_correctly"], s["n"]),
             _pc(g["refused_wrongly"], s["n"])) if s["n_graded"] else (NM,) * 4
        tpa = f"{_ms(s['tokens_per_answer'])} / {_ms(s['tokens_per_model_answer'])}"
        mt = res.get("model_ttft", {}).get(sc) or {"p50": None, "p95": None}
        L.append(f"| {key}: {desc} | {s['n']} | {q[0]} | {q[1]} | {q[2]} | {q[3]} | {tpa} | {_pair(mt)} | "
                 f"{_pair(s['latency']['ft'])} | {_pair(s['latency']['total'])} |")
    if res["retrieval"]:
        rv = res["retrieval"]["settings"]
        L += ["", f"## Table 3b: Retrieval only, all settings (offline, free; from {res['retrieval']['file']})", "",
              "| Setting | Answerable questions | Mean evidence recall | All evidence retrieved | Answerable wrongly abstained | Unanswerable reaching the model | Mean context tokens per question reaching the model |",
              "|---|---|---|---|---|---|---|"]
        for key in sorted(rv):
            r = rv[key]
            L.append(f"| {key}: {res['settings_desc'].get(key, key)} | {r['answerable']} | {r['recall_sum'] / max(1, r['answerable']):.2f} | "
                     f"{r['hit_all']} | {r['answerable_abstained']} | {r['unanswerable_reach_model']} | {r['ctx'] / max(1, r['reach']):.0f} |")
    s = res["slowest"]
    if s:
        L += ["", f"## Slowest successful stand-in request ({s['scenario']}, run {s['run_id']}, {s['request_id']})", "",
              f"Total {s['total_ms']:.1f} ms; first token at {s['first_token_ms']:.1f} ms; **{s['after_first_token_ms']:.1f} ms after the first token** "
              f"(the answer is buffered and verified before release, so the delay is before the first token).",
              f"waiting {s['waiting_ms']:.1f} ms, working {s['working_ms']:.1f} ms, retrying {s['retrying_ms']:.1f} ms, "
              f"unaccounted {s['unaccounted_ms']:.1f} ms; retries {s['retries']}.", "", "| t (ms) | event | detail |", "|---|---|---|"]
        for e in s["timeline"]:
            d = {k: v for k, v in e.items() if k not in ("t_ms", "event")}
            L.append(f"| {e['t_ms']} | {e['event']} | {', '.join(f'{k}={v}' for k, v in d.items())} |")
    L.append("")
    return "\n".join(L)
