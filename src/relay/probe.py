"""Probe the real service in small, budgeted stages (DESIGN.md 8.1).

    python -m relay.cli probe --stage single --allow-real

Every call is appended, raw, to results/probe_real.jsonl. That file is the only evidence for
anything labelled OBSERVED-REPORTED (the provider said so in headers or body) or
OBSERVED-TRIGGERED (we hit the limit and recorded the response).

Stages, cheapest first. Run them in this order and read each result before the next.
  single       1 call: do the request parameters work, which headers come back, where is usage?
  latency      n sequential calls, small and ~1.5K-token prompts: TTFT and decode speed
  concurrency  1, 2 and 4 simultaneous small calls: does latency grow with concurrency?
  limit        a rapid burst of tiny calls until the FIRST 429, then recovery: the per-minute limit's
               response, headers, retry-after and recovery time (OBSERVED-TRIGGERED)

Safety: a stage refuses to start without --allow-real; every call checks the daily ledger
(results/ledger_*.json) and stops when its cap is reached; the limit stage stops at the first 429
and never approaches the daily limit. Tokens are counted from provider usage where present.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .gateway import DailyLedger
from .llm_client import CallResult, LLMClient
from .metrics import percentile
from .schema import RelayError

STAGES = ("single", "latency", "concurrency", "limit")
PAD_SENTENCE = "The quick brown fox jumps over the lazy dog near the quiet river bank. "
PLANNED_TOKENS = {"single": 400, "latency": 12000, "concurrency": 6000, "limit": 4500}   # rough upper estimates


@dataclass
class ProbeEnv:
    client: LLMClient
    ledger: DailyLedger
    out: Path
    model: str
    source: str            # "real" | "stand-in" (the latter only in tests)


def _pad(approx_tokens: int) -> str:
    return PAD_SENTENCE * max(1, approx_tokens // 16)


def _record(env: ProbeEnv, stage: str, i: int, res: CallResult, prompt_chars: int, extra: Optional[dict] = None) -> dict:
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "stage": stage, "i": i,
        "source": env.source, "model": env.model, "ok": res.ok, "error_kind": res.error_kind,
        "status_code": res.status_code, "classified_by": res.classified_by, "retry_after_s": res.retry_after_s,
        "message": res.message[:300], "first_byte_ms": res.first_byte_ms, "ttft_ms": res.ttft_ms,
        "total_ms": round(res.total_ms, 1), "tokens_in": res.tokens_in, "tokens_out": res.tokens_out,
        "usage_source": res.usage_source, "finish_reason": res.finish_reason, "headers": res.headers,
        "text_preview": res.text[:80], "prompt_chars": prompt_chars,
        "provenance": "OBSERVED-REPORTED" if env.source == "real" else "SIMULATED (stand-in test)",
    }
    if extra:
        row.update(extra)
    env.out.parent.mkdir(parents=True, exist_ok=True)
    with open(env.out, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return row


async def _call(env: ProbeEnv, stage: str, i: int, prompt: str, max_tokens: int, extra: Optional[dict] = None,
                timeout_s: float = 60.0) -> tuple:
    if env.ledger.exhausted:
        raise RelayError("daily ledger cap reached; stopping the probe")
    res = await env.client.chat([{"role": "user", "content": prompt}], max_tokens=max_tokens, timeout_s=timeout_s)
    env.ledger.record(res.tokens_in + res.tokens_out if res.ok or res.usage_source == "provider" else 0)
    return res, _record(env, stage, i, res, len(prompt), extra)


def _line(row: dict) -> str:
    return (f"  #{row['i']:<3} {'ok ' if row['ok'] else (row['error_kind'] or 'err')[:14]:<14} http={row['status_code']} "
            f"ttft={_f(row['ttft_ms'])} total={_f(row['total_ms'])} in={row['tokens_in']} out={row['tokens_out']} "
            f"usage={row['usage_source']}")


def _f(v: Any) -> str:
    return "n/a" if v is None else f"{v:.0f}ms"


async def stage_single(env: ProbeEnv) -> list:
    res, row = await _call(env, "single", 0, "Reply with exactly one word: ready.", 300)
    lines = [_line(row), f"  reply: {res.text!r}   finish_reason: {res.finish_reason}",
             f"  headers returned: {sorted(res.headers) or 'none'}"]
    if not res.ok:
        lines.append(f"  FAILED: {res.error_kind}: {res.message}")
        lines.append("  If this says a parameter is unsupported (for example reasoning_effort), edit config/relay.yaml.")
    elif res.usage_source != "provider":
        lines.append("  NOTE: no usage found in the stream. Token counts will be ESTIMATES until this is resolved.")
    return lines


async def stage_latency(env: ProbeEnv, n: int, big: int) -> list:
    rows = []
    big_at = {int(k * n / max(1, big)) for k in range(big)} if big else set()
    for i in range(n):
        is_big = i in big_at
        prompt = (_pad(1500) + "\n\nIn one sentence, what animal is mentioned above?") if is_big else "Name one primary colour in one sentence."
        res, row = await _call(env, "latency", i, prompt, 150, {"size": "big" if is_big else "small"})
        rows.append(row)
        if is_big:
            await asyncio.sleep(10)        # keep the minute's token use well under the TPM limit
        else:
            await asyncio.sleep(2)
    return [_line(r) + f" [{r['size']}]" for r in rows] + _summary(rows)


async def stage_concurrency(env: ProbeEnv, reps: int) -> list:
    out: list = []
    for c in (1, 2, 4):
        rows = []
        for rep in range(reps):
            batch = await asyncio.gather(*[
                _call(env, "concurrency", rep * c + k, "Name one primary colour in one sentence.", 100, {"concurrency": c})
                for k in range(c)])
            rows += [r for _, r in batch]
            await asyncio.sleep(3)
        out.append(f"  concurrency {c}: " + "; ".join(_summary(rows)))
    return out


async def stage_limit(env: ProbeEnv, max_calls: int, recovery: bool) -> list:
    """Send tiny calls as fast as possible until the first 429, then measure recovery."""
    lines: list = []
    first429: Optional[CallResult] = None
    t_first = 0.0
    sent = 0
    batch_n = 6
    while sent < max_calls and first429 is None:
        batch = await asyncio.gather(*[
            _call(env, "limit", sent + k, "Say ok.", 64, {"phase": "burst"}) for k in range(batch_n)])
        sent += batch_n
        for res, row in batch:
            if not res.ok and (res.error_kind or "").startswith("rate_limit") and first429 is None:
                first429, t_first = res, time.monotonic()
                lines.append("  FIRST 429: " + _line(row))
                lines.append(f"    message: {res.message}")
                lines.append(f"    retry-after: {res.retry_after_s}   classified as {res.error_kind} by {res.classified_by}")
                lines.append(f"    headers: {res.headers}")
    lines.append(f"  calls sent before stopping: {sent} (the documented RPM is 30)")
    if first429 is None:
        lines.append("  no 429 within the cap. The limit was NOT triggered; Table 1 must say so.")
        return lines
    if recovery:
        wait = (first429.retry_after_s or 5.0)
        await asyncio.sleep(wait)
        t_end = time.monotonic() + 90
        attempt = 0
        while time.monotonic() < t_end:
            res, row = await _call(env, "limit", sent + attempt, "Say ok.", 64, {"phase": "recovery"})
            attempt += 1
            if res.ok:
                lines.append(f"  recovered {time.monotonic() - t_first:.1f}s after the first 429 "
                             f"(first retry after {wait:.0f}s retry-after; attempts: {attempt})")
                break
            lines.append("  recovery attempt failed: " + _line(row))
            await asyncio.sleep(5)
        else:
            lines.append("  did not recover within 90 s")
    return lines


def _summary(rows: list) -> list:
    ok = [r for r in rows if r["ok"]]
    if not ok:
        return ["no successful calls"]
    ttft = [r["ttft_ms"] for r in ok if r["ttft_ms"] is not None]
    tot = [r["total_ms"] for r in ok]
    tout = [r["tokens_out"] for r in ok]
    bits = [f"ok {len(ok)}/{len(rows)}"]
    if ttft:
        bits.append(f"ttft p50={percentile(ttft, 50):.0f} max={max(ttft):.0f}ms")
    bits.append(f"total p50={percentile(tot, 50):.0f} max={max(tot):.0f}ms")
    bits.append(f"tokens_out mean={statistics.fmean(tout):.0f}")
    return ["  " + "   ".join(bits)]


async def run_probe(env: ProbeEnv, stage: str, *, n: int = 10, big: int = 4, reps: int = 3,
                    max_calls: int = 40, recovery: bool = True) -> list:
    if stage not in STAGES:
        raise RelayError(f"unknown stage {stage}. Choose from {STAGES}")
    if stage == "single":
        return await stage_single(env)
    if stage == "latency":
        return await stage_latency(env, n, big)
    if stage == "concurrency":
        return await stage_concurrency(env, reps)
    return await stage_limit(env, max_calls, recovery)
