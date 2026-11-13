"""Pure functions over events: per-request rows, summaries, invariants.

Nothing here does I/O or imports the runner, the gateway or any network code, so the
CLI summary, `results.csv` and (later) the dashboard and `analyze` all compute from the
same logic and cannot disagree. Missing data is None with a reason, never 0.

waiting / working / retrying split of one request (DESIGN.md 8.3), from events:
  waiting   door-queue wait + limiter waits (a request shed or timed out in the queue was waiting throughout)
  working   successful call durations + tool time + V0 time (+ a call cut short by the deadline)
  retrying  retry back-off sleeps + the duration of failed calls
  unaccounted  total minus the three; shown as is, never forced to sum
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Optional, Sequence

CSV_FIELDS = (
    "request_id", "scenario", "source", "start_time", "first_token_ms", "total_ms", "status", "retries",
    "tokens_in", "tokens_out", "retrieval_setting", "answer_grade",
)


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Linear interpolation between closest ranks (the usual 'type 7' definition). q in [0, 100]."""
    xs = sorted(values)
    if not xs:
        return None
    if len(xs) == 1:
        return float(xs[0])
    pos = (len(xs) - 1) * q / 100.0
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return float(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo))


def dist_summary(values: Sequence[float]) -> dict[str, Any]:
    n = len(values)
    if n == 0:
        return {"n": 0, "p50": None, "p95": None, "p99": None, "max": None, "mean": None,
                "note": "no samples"}
    out: dict[str, Any] = {
        "n": n, "p50": percentile(values, 50), "p95": percentile(values, 95),
        "p99": percentile(values, 99), "max": float(max(values)), "mean": sum(values) / n, "note": None,
    }
    if n < 20:
        out["note"] = "n < 20: percentiles are indicative only"
    if n < 100:
        out["p99"] = None
        out["p99_note"] = "too few samples for p99 (n < 100)"
    return out


def _iso_add(start_iso: str, seconds: float) -> str:
    return (datetime.fromisoformat(start_iso) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def group_by_request(events: Iterable[Mapping[str, Any]]) -> dict[str, list]:
    by: dict[str, list] = defaultdict(list)
    for e in events:
        rid = e.get("request_id")
        if rid is not None:
            by[rid].append(e)
    return by


def request_row(evs: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """One row from one request's events, or None if the request never finished (no `done` event)."""
    done = next((e for e in evs if e["event"] == "done"), None)
    if done is None:
        return None
    arrive = next((e for e in evs if e["event"] == "arrive"), evs[0])
    t_arrive, t_done = arrive["t_mono"], done["t_mono"]
    total_ms = done.get("total_ms")
    if total_ms is None:
        total_ms = (t_done - t_arrive) * 1000.0

    waiting = working = retrying = 0.0
    open_send: Optional[float] = None
    open_sleep: Optional[float] = None
    open_wait: Optional[float] = None
    shed = bool(done.get("shed_reason"))
    for e in evs:
        ev = e["event"]
        if ev == "slot_acquired":
            waiting += float(e.get("wait_ms", 0.0))
        elif ev == "limiter_wait_start":
            open_wait = e["t_mono"]
        elif ev == "limiter_wait_end":
            waiting += float(e.get("waited_ms", 0.0))
            open_wait = None
        elif ev == "send":
            open_send = e["t_mono"]
        elif ev == "call_end":
            open_send = None
            if e.get("ok"):
                working += float(e.get("duration_ms", 0.0))
            else:
                retrying += float(e.get("duration_ms", 0.0))
        elif ev == "retry_sleep_start":
            open_sleep = e["t_mono"]
            retrying += float(e.get("sleep_ms", 0.0))
        elif ev == "retry_sleep_end":
            open_sleep = None
        elif ev == "tool_end":
            working += float(e.get("duration_ms", 0.0))
        elif ev == "verify_result":
            working += float(e.get("ms", 0.0))
    # intervals cut short by the deadline (no closing event)
    if open_send is not None:
        working += (t_done - open_send) * 1000.0
    if open_sleep is not None:
        retrying -= float(next((e.get("sleep_ms", 0.0) for e in evs if e["event"] == "retry_sleep_start" and e["t_mono"] == open_sleep), 0.0))
        retrying += (t_done - open_sleep) * 1000.0
    if open_wait is not None:
        waiting += (t_done - open_wait) * 1000.0
    if shed or (done.get("fail_stage") == "admit"):
        waiting = total_ms
    model_ft = next((e.get("ttft_ms") for e in evs if e["event"] == "model_first_token"), None)

    return {
        "request_id": done["request_id"],
        "scenario": manifest.get("scenario"),
        "source": manifest.get("source"),
        "start_time": _iso_add(manifest["wall_start_utc"], t_arrive),
        "first_token_ms": done.get("first_token_ms"),
        "total_ms": round(total_ms, 1),
        "status": done["status"],
        "retries": done.get("retries", 0),
        "tokens_in": done.get("tokens_in", 0),
        "tokens_out": done.get("tokens_out", 0),
        "retrieval_setting": done.get("retrieval_setting"),
        "answer_grade": None,                      # never invented; filled only by real, reviewed quality runs
        # extras (not in results.csv)
        "question_id": done.get("question_id"),
        "answer_mode": done.get("answer_mode"),
        "shed_reason": done.get("shed_reason"),
        "fail_stage": done.get("fail_stage"),
        "fail_reason": done.get("fail_reason"),
        "ladder_level": done.get("ladder_level"),
        "attempts": done.get("attempts", 0),
        "discretionary_calls": done.get("discretionary_calls", 0),
        "regenerated": done.get("regenerated", False),
        "v0_passed": done.get("v0_passed"),
        "tokens_unaccounted_est": done.get("tokens_unaccounted_est", 0),
        "model_first_token_ms": model_ft,
        "arrive_t": t_arrive,
        "waiting_ms": round(waiting, 1), "working_ms": round(working, 1), "retrying_ms": round(retrying, 1),
        "unaccounted_ms": round(total_ms - waiting - working - retrying, 1),
    }


def request_rows(events: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any]) -> list:
    rows = []
    for rid, evs in group_by_request(events).items():
        row = request_row(evs, manifest)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda r: r["arrive_t"])
    return rows


def csv_rows(rows: Sequence[Mapping[str, Any]]) -> list:
    return [{k: r.get(k) for k in CSV_FIELDS} for r in rows]


def summarize(rows: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]],
              manifest: Mapping[str, Any]) -> dict[str, Any]:
    n = len(rows)
    by_status: dict[str, int] = defaultdict(int)
    modes: dict[str, int] = defaultdict(int)
    sheds: dict[str, int] = defaultdict(int)
    for r in rows:
        by_status[r["status"]] += 1
        if r["status"] == "ok":
            modes[r.get("answer_mode") or "unknown"] += 1
        if r.get("shed_reason"):
            sheds[r["shed_reason"]] += 1
    ok = [r for r in rows if r["status"] == "ok"]
    started = {e["request_id"] for e in events if e["event"] == "arrive"}
    finished = {r["request_id"] for r in rows}
    states = [e for e in events if e["event"] == "gateway_state"]
    peak_queue = max((e.get("queue_len", 0) for e in states), default=None)
    peak_inflight = max((e.get("inflight", 0) for e in states), default=None)
    t_in = sum(r["tokens_in"] or 0 for r in rows)
    t_out = sum(r["tokens_out"] or 0 for r in rows)
    pr = manifest.get("prices") or {}
    cost = None
    if pr.get("in_per_mtok") is not None and pr.get("out_per_mtok") is not None:
        cost = (t_in * pr["in_per_mtok"] + t_out * pr["out_per_mtok"]) / 1e6
    llm_ok = [r for r in ok if r.get("answer_mode") == "llm"]
    duration = manifest.get("load_duration_s")
    return {
        "provenance": manifest.get("provenance"),
        "source": manifest.get("source"),
        "scenario": manifest.get("scenario"),
        "n_requests": n,
        "n_incomplete": len(started - finished),
        "status_counts": {s: by_status.get(s, 0) for s in ("ok", "rate_limited", "timeout", "error")},
        "ok_rate": (len(ok) / n) if n else None,
        "ok_by_answer_mode": dict(modes),
        "degraded_ok": modes.get("extractive", 0),      # refusals and cache hits are not degradation
        "shed_reasons": dict(sheds),
        "goodput_per_min": (len(ok) / duration * 60.0) if duration else None,
        "first_token_ms": dist_summary([r["first_token_ms"] for r in ok if r["first_token_ms"] is not None]),
        "first_token_ms_llm": dist_summary([r["first_token_ms"] for r in llm_ok if r["first_token_ms"] is not None]),
        "total_ms_llm": dist_summary([r["total_ms"] for r in llm_ok]),
        "total_ms_ok": dist_summary([r["total_ms"] for r in ok]),
        "total_ms_all": dist_summary([r["total_ms"] for r in rows]),
        "retries_total": sum(r["retries"] or 0 for r in rows),
        "requests_with_retries": sum(1 for r in rows if (r["retries"] or 0) > 0),
        "discretionary_calls_total": sum(r["discretionary_calls"] or 0 for r in rows),
        "attempts_total": sum(r["attempts"] or 0 for r in rows),
        "tokens_in": t_in, "tokens_out": t_out,
        "tokens_unaccounted_est": sum(r["tokens_unaccounted_est"] or 0 for r in rows),
        "tokens_per_answer": ((t_in + t_out) / len(llm_ok)) if llm_ok else None,
        "notional_cost_usd": cost,
        "notional_cost_per_1000_answers_usd": (cost / len(ok) * 1000.0) if (cost is not None and ok) else None,
        "peak_queue_len": peak_queue,
        "peak_inflight": peak_inflight,
        "v0_rejections": sum(1 for r in rows if r.get("v0_passed") is False or r.get("regenerated")),
        "regenerations": sum(1 for r in rows if r.get("regenerated")),
    }


def _inv(ident: str, description: str, applicable: bool, violations: list, checked: int) -> dict[str, Any]:
    return {"id": ident, "description": description, "applicable": applicable,
            "passed": (len(violations) == 0) if applicable else None,
            "checked": checked, "violations": violations[:5], "n_violations": len(violations)}


def check_invariants(events: Sequence[Mapping[str, Any]], params: Mapping[str, Any]) -> list:
    """The six invariants of DESIGN.md 4.3, recomputed from events.

    params: max_attempts, n_inflight, reserve_s, discretionary_share (None = not applicable),
            global_pause (bool), slack (default 0.10), min_window_calls (default 20).
    """
    sends = [e for e in events if e["event"] == "send"]
    out = []
    # I1: no provider attempt starts when no time is left for the mandatory later steps.
    floor = float(params.get("reserve_s", 0.0)) * 1000.0
    v = [e for e in sends if e.get("remaining_ms", 0.0) < floor]
    out.append(_inv("I1", "no provider attempt starts without time left for V0 and release", True, [
        {"request_id": e["request_id"], "t_mono": e["t_mono"], "remaining_ms": e.get("remaining_ms")} for e in v], len(sends)))
    # I2: attempts per request <= K
    per: dict[str, int] = defaultdict(int)
    for e in sends:
        per[e["request_id"]] += 1
    k = params.get("max_attempts")
    v2 = [{"request_id": r, "attempts": c} for r, c in per.items() if k is not None and c > k]
    out.append(_inv("I2", "provider attempts per request <= K", k is not None, v2, len(per)))
    # I3: discretionary share of attempts in any 60 s window
    share = params.get("discretionary_share")
    v3: list = []
    if share is not None:
        slack = float(params.get("slack", 0.10))
        minc = int(params.get("min_window_calls", 20))
        ts = [(e["t_mono"], bool(e.get("discretionary"))) for e in sends]
        lo = 0
        disc = 0
        for hi in range(len(ts)):
            disc += 1 if ts[hi][1] else 0
            while ts[hi][0] - ts[lo][0] > 60.0:
                disc -= 1 if ts[lo][1] else 0
                lo += 1
            total = hi - lo + 1
            if total >= minc and disc / total > share + slack:
                v3.append({"t_mono": ts[hi][0], "discretionary": disc, "total": total})
                break
    out.append(_inv("I3", "discretionary attempts <= share + slack in any 60 s window (windows with >= 20 calls)",
                    share is not None, v3, len(sends)))
    # I4: pipelines in flight <= N
    n = params.get("n_inflight")
    marks = sorted(
        [(e["t_mono"], 1 if e["event"] == "slot_acquired" else -1) for e in events
         if e["event"] in ("slot_acquired", "slot_released")],
        key=lambda m: (m[0], m[1]))        # releases sort before acquisitions at the same instant
    cur = peak = 0
    for _, d in marks:
        cur += d
        peak = max(peak, cur)
    v4 = [{"peak": peak, "limit": n}] if (n is not None and peak > n) else []
    out.append(_inv("I4", "concurrent pipelines <= N", n is not None, v4, len(marks)))
    # I5: nobody is admitted to a slot after their deadline
    acq = [e for e in events if e["event"] == "slot_acquired"]
    v5 = [{"request_id": e["request_id"], "remaining_ms": e.get("remaining_ms")} for e in acq if e.get("remaining_ms", 1.0) <= 0]
    out.append(_inv("I5", "no request is admitted after its deadline", True, v5, len(acq)))
    # I6: no dispatch while the global pause is active
    gp = bool(params.get("global_pause", True))
    pauses = [(e["t_mono"], e["until_mono"]) for e in events if e["event"] == "pause_start"]
    v6 = []
    if gp:
        for e in sends:
            for a, b in pauses:
                if a + 1e-3 < e["t_mono"] < b - 1e-3:
                    v6.append({"request_id": e["request_id"], "t_mono": e["t_mono"], "pause": [a, b]})
                    break
    out.append(_inv("I6", "no provider attempt starts during a global pause", gp, v6, len(sends)))
    return out
