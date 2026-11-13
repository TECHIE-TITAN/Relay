"""OpenAI-compatible stand-in model server.

    python -m relay.standin.server --port 8089

Endpoints
  POST /v1/chat/completions   streaming (SSE) and non-streaming
  POST /control               change behaviour at runtime: {"set": {...}, "reset": true}
  GET  /stats                 server-side truth: counts by outcome, in-flight, usage windows
  GET  /health

Behaviour (all parameters SIMULATED, see profile.py): lognormal time-to-first-token,
streamed decode at a fixed rate, latency growing with in-flight requests beyond a
knee, per-minute and per-day request/token limits that answer 429 with the same
header names the Groq docs describe, 5xx, hangs, slow successes, mid-stream drops.

The reply quotes the first provided source (`[S1]` block of the prompt) and cites
it. Its content is fake: NO QUALITY NUMBER may ever be taken from this server.

It never reads or logs an Authorization header.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import time
import uuid
from collections import deque
from typing import Any, Callable, Deque, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..textutil import approx_tokens
from .profile import StandinProfile

REFUSAL = "The papers do not say."
_S1_RE = re.compile(r"\[S1\][^\n]*\n(.+?)(?:\n\n\[S\d+\]|\n\nQuestion:|\Z)", re.DOTALL)


class _Window:
    """Amounts recorded in the last `span` seconds."""

    def __init__(self, span_s: float = 60.0) -> None:
        self.span = span_s
        self.items: Deque[list] = deque()

    def _trim(self, now: float) -> None:
        while self.items and now - self.items[0][0] >= self.span:
            self.items.popleft()

    def used(self, now: float) -> float:
        self._trim(now)
        return float(sum(i[1] for i in self.items))

    def add(self, now: float, amount: float) -> list:
        entry = [now, amount]
        self.items.append(entry)
        return entry

    def resets_in(self, now: float) -> float:
        self._trim(now)
        return max(0.0, self.span - (now - self.items[0][0])) if self.items else 0.0


class StandinState:
    def __init__(self, profile: StandinProfile, now: Callable[[], float] = time.monotonic) -> None:
        import random

        self.profile = profile
        self.now = now
        self.rng = random.Random(profile.seed)
        self.rpm_win = _Window()
        self.tpm_win = _Window()
        self.rpd_used = 0
        self.tpd_used = 0
        self.inflight = 0
        self.peak_inflight = 0
        self.counts: dict[str, int] = {}

    def count(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def reset(self) -> None:
        self.rpm_win = _Window()
        self.tpm_win = _Window()
        self.rpd_used = 0
        self.tpd_used = 0
        self.peak_inflight = self.inflight
        self.counts = {}


def _dur(seconds: float) -> str:
    return f"{max(0.0, seconds):.2f}s"


def limit_headers(state: StandinState, now: float) -> dict[str, str]:
    """Header names as documented: *-requests refer to RPD, *-tokens to TPM."""
    p = state.profile
    h: dict[str, str] = {}
    if p.rpd:
        h["x-ratelimit-limit-requests"] = str(p.rpd)
        h["x-ratelimit-remaining-requests"] = str(max(0, p.rpd - state.rpd_used))
        h["x-ratelimit-reset-requests"] = _dur(p.daily_retry_after_s)
    if p.tpm:
        h["x-ratelimit-limit-tokens"] = str(p.tpm)
        h["x-ratelimit-remaining-tokens"] = str(max(0, int(p.tpm - state.tpm_win.used(now))))
        h["x-ratelimit-reset-tokens"] = _dur(state.tpm_win.resets_in(now))
    return h


def _error(status: int, message: str, etype: str, headers: Optional[dict[str, str]] = None) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": etype}}, status_code=status, headers=headers)


_ABBR = {
    ("requests", "minute"): "RPM", ("requests", "day"): "RPD",
    ("tokens", "minute"): "TPM", ("tokens", "day"): "TPD",
}


def _rate_limited(state: StandinState, model: str, kind: str, scope: str, now: float) -> JSONResponse:
    p = state.profile
    if scope == "day":
        retry_after = p.daily_retry_after_s
    else:
        win = state.rpm_win if kind == "requests" else state.tpm_win
        retry_after = max(1.0, win.resets_in(now))
    wait = int(math.ceil(retry_after))
    headers = limit_headers(state, now)
    headers["retry-after"] = str(wait)
    state.count(f"429_{scope}")
    msg = (
        f"Rate limit reached for model `{model}` on {kind} per {scope} ({_ABBR[(kind, scope)]}). "
        f"Please try again in {wait}s. [stand-in server: simulated limit]"
    )
    return _error(429, msg, kind, headers)


def _reply(messages: list, p: StandinProfile, bad_citation: bool) -> str:
    """Quote the first provided source with a citation; refuse if none was provided."""
    user_text = ""
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            user_text = str(m.get("content", ""))
            break
    m1 = _S1_RE.search(user_text)
    if not m1:
        return REFUSAL
    words = m1.group(1).split()[: max(1, p.reply_words)]
    excerpt = " ".join(words)
    excerpt = re.sub(r"[.!?]+(\s)", r",\1", excerpt).rstrip(" .!?,;:")   # one sentence, one citation
    return f"{excerpt} [{'S9' if bad_citation else 'S1'}]."


def _sse(obj: Any) -> str:
    return "data: " + json.dumps(obj, separators=(",", ":")) + "\n\n"


def create_app(profile: Optional[StandinProfile] = None, *, now: Callable[[], float] = time.monotonic) -> FastAPI:
    state = StandinState(profile or StandinProfile(), now)
    app = FastAPI(title="Relay stand-in model server", docs_url=None, redoc_url=None)
    app.state.standin = state

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "kind": "stand-in", "provenance": "SIMULATED"}

    @app.get("/stats")
    async def stats() -> dict:
        t = state.now()
        return {
            "provenance": "SIMULATED (stand-in server-side counters)",
            "counts": dict(state.counts),
            "inflight": state.inflight,
            "peak_inflight": state.peak_inflight,
            "rpd_used": state.rpd_used,
            "tpd_used": state.tpd_used,
            "rpm_window_used": state.rpm_win.used(t),
            "tpm_window_used": state.tpm_win.used(t),
            "profile": state.profile.to_dict(),
        }

    @app.post("/control")
    async def control(request: Request) -> Any:
        try:
            body = await request.json()
            if body.get("set"):
                state.profile.update(body["set"])
            if body.get("reset"):
                state.reset()
        except (ValueError, TypeError, AttributeError) as e:
            return _error(400, str(e), "invalid_request_error")
        return {"ok": True, "profile": state.profile.to_dict()}

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Any:
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "request body is not valid JSON", "invalid_request_error")
        messages = body.get("messages") if isinstance(body, dict) else None
        if not isinstance(messages, list) or not messages:
            return _error(400, "'messages' must be a non-empty list", "invalid_request_error")
        stream = bool(body.get("stream"))
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        max_out = int(body.get("max_completion_tokens") or body.get("max_tokens") or 1024)
        model = str(body.get("model", "stand-in"))
        tokens_in = approx_tokens(" ".join(str(m.get("content", "")) for m in messages if isinstance(m, dict)))
        reserve = tokens_in + max_out   # SIMULATED choice: max output is counted up front
        p = state.profile
        t = state.now()
        state.count("received")

        if p.rpd and state.rpd_used >= p.rpd:
            return _rate_limited(state, model, "requests", "day", t)
        if p.tpd and state.tpd_used + tokens_in > p.tpd:
            return _rate_limited(state, model, "tokens", "day", t)
        if p.tpm and reserve > p.tpm:
            state.count("413")
            return _error(413, f"request needs {reserve} tokens, above the per-minute limit {p.tpm}", "invalid_request_error")
        if p.rpm and state.rpm_win.used(t) + 1 > p.rpm:
            return _rate_limited(state, model, "requests", "minute", t)
        if p.tpm and state.tpm_win.used(t) + reserve > p.tpm:
            return _rate_limited(state, model, "tokens", "minute", t)

        u = [state.rng.random() for _ in range(5)]   # 5xx, hang, slow, midstream, bad citation
        noise = state.rng.lognormvariate(0.0, p.ttft_sigma)
        if u[0] < p.p_5xx:
            state.count("5xx")
            await asyncio.sleep(0.05)
            return _error(503, "simulated upstream failure", "server_error")
        if u[1] < p.p_hang:
            state.count("hang")
            await asyncio.sleep(p.hang_s)
            return _error(504, "simulated hang timed out", "server_error")

        entry_t = state.tpm_win.add(t, reserve)
        state.rpm_win.add(t, 1)
        state.rpd_used += 1
        state.inflight += 1
        state.peak_inflight = max(state.peak_inflight, state.inflight)
        mult = 1.0 + p.slowdown_alpha * max(0, state.inflight - p.knee_inflight)
        if u[2] < p.p_slow:
            state.count("slow")
            mult *= p.slow_factor
        ttft_s = (p.ttft_median_ms / 1000.0) * noise * mult
        content = _reply(messages, p, bad_citation=u[4] < p.p_bad_citation)
        out_tokens = max(1, approx_tokens(content))
        per_token_s = mult / max(1e-6, p.tokens_per_s)
        usage = {"prompt_tokens": tokens_in, "completion_tokens": out_tokens, "total_tokens": tokens_in + out_tokens}
        cid = "chatcmpl-" + uuid.uuid4().hex[:12]
        created = int(time.time())
        headers = limit_headers(state, t)

        def settle(out_used: int) -> None:
            entry_t[1] = tokens_in + out_used
            state.tpd_used += tokens_in + out_used
            state.inflight -= 1

        if not stream:
            try:
                await asyncio.sleep(ttft_s + out_tokens * per_token_s)
            except BaseException:
                settle(0)
                raise
            settle(out_tokens)
            state.count("ok")
            return JSONResponse(
                {"id": cid, "object": "chat.completion", "created": created, "model": model,
                 "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                 "usage": usage},
                headers=headers,
            )

        def chunk(delta: dict, finish: Optional[str] = None) -> dict:
            return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        async def gen():
            emitted = 0
            completed = False
            try:
                await asyncio.sleep(ttft_s)
                yield _sse(chunk({"role": "assistant", "content": ""}))
                words = content.split(" ")
                n = max(1, p.stream_chunk_words)
                groups = [words[i:i + n] for i in range(0, len(words), n)]
                drop_at = len(groups) // 2 if u[3] < p.p_midstream else None
                for gi, g in enumerate(groups):
                    if drop_at is not None and gi == drop_at:
                        state.count("midstream_drop")
                        return   # ends the stream with no finish_reason and no [DONE]
                    text = " ".join(g) + (" " if gi < len(groups) - 1 else "")
                    yield _sse(chunk({"content": text}))
                    emitted += max(1, approx_tokens(text))
                    await asyncio.sleep(max(1.0, len(g) * 1.3) * per_token_s)
                completed = True
                final = chunk({}, "stop")
                if include_usage:
                    yield _sse(final)
                    yield _sse({"id": cid, "object": "chat.completion.chunk", "created": created,
                                "model": model, "choices": [], "usage": usage})
                else:
                    final["x_groq"] = {"usage": usage}
                    yield _sse(final)
                yield "data: [DONE]\n\n"
                state.count("ok")
            finally:
                settle(out_tokens if completed else emitted)

        return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)

    return app


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="relay.standin.server", description="Relay stand-in model server (SIMULATED)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8089)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--limits-scale", type=float, default=1.0, help="multiply rpm/tpm/rpd/tpd by this")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override a profile field, e.g. --set p_5xx=0.1 (repeatable)")
    args = ap.parse_args(argv)
    profile = StandinProfile(seed=args.seed)
    overrides = {}
    for item in args.set:
        if "=" not in item:
            ap.error(f"--set expects KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        overrides[k] = v
    profile.update(overrides)
    profile = profile.scaled_limits(args.limits_scale)
    import uvicorn

    uvicorn.run(create_app(profile), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
