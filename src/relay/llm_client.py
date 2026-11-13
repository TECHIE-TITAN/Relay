"""Streaming chat-completions client (OpenAI-compatible: the stand-in or Groq).

One call = one HTTP attempt. No retries, no rate limiting and no budget here:
those belong to the gateway. The client reports exactly what happened.

Measured per call: time to response headers (`first_byte_ms`), time to the first
user-visible content token (`ttft_ms`; reasoning deltas do not count), total time.
Token usage is read from the provider (top-level `usage`, or `x_groq.usage`, since
the docs do not say where a streaming reply carries it); if neither is present
the call is marked `usage_source="estimated"` (characters / 4) and must not be
reported as a provider figure.

Error classification (error_kind)
  rate_limit_minute / rate_limit_day   HTTP 429. The split is a GUESS (the docs do not
                                       say how the two differ): a retry-after above
                                       `day_retry_after_s`, or "per day"/(RPD)/(TPD) in the
                                       message, means "day". `classified_by` records which.
  server_error      5xx, 424, 498     retryable
  client_error      other 4xx (413 too large, 400, 401 ...)   not retryable
  timeout           whole-call timeout        first_token_timeout   no token in time
  connect           could not connect         midstream             stream broke or ended early
  empty_reply       finished with no content (not retryable)
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import httpx

from .textutil import approx_tokens

RETRYABLE = frozenset({"rate_limit_minute", "server_error", "timeout", "first_token_timeout", "connect", "midstream"})


@dataclass
class CallResult:
    ok: bool
    text: str = ""
    status_code: Optional[int] = None
    error_kind: Optional[str] = None
    message: str = ""
    retry_after_s: Optional[float] = None
    classified_by: Optional[str] = None
    tokens_in: int = 0
    tokens_out: int = 0
    usage_source: str = "none"          # provider | estimated | none
    first_byte_ms: Optional[float] = None
    ttft_ms: Optional[float] = None
    total_ms: float = 0.0
    headers: dict = field(default_factory=dict)   # x-ratelimit-* and retry-after only
    finish_reason: Optional[str] = None
    reasoning_tokens: Optional[int] = None   # provider-reported part of tokens_out spent on reasoning, if given


@dataclass
class _State:
    t0: float
    first_byte: Optional[float] = None
    first_token: Optional[float] = None
    text: str = ""
    usage: Optional[dict] = None
    finish_reason: Optional[str] = None
    done: bool = False
    status: Optional[int] = None
    headers: dict = field(default_factory=dict)


class _FirstTokenTimeout(Exception):
    pass


def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def classify_429(headers: dict, message: str, day_retry_after_s: float) -> tuple:
    """Returns (error_kind, classified_by). A guess, labelled as such (see module docstring)."""
    msg = (message or "").lower()
    ra = _to_float(headers.get("retry-after"))
    if re.search(r"per day|\((?:rpd|tpd)\)", msg):
        return "rate_limit_day", "message"
    if ra is not None and ra > day_retry_after_s:
        return "rate_limit_day", "retry_after"
    if re.search(r"per minute|\((?:rpm|tpm)\)", msg):
        return "rate_limit_minute", "message"
    return "rate_limit_minute", "default"


class LLMClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
        extra_params: Optional[dict] = None,
        stream_options: bool = True,
        day_retry_after_s: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._key = api_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0))
        self._owns_client = client is None
        self.extra_params = dict(extra_params or {})
        self.stream_options = stream_options
        self.day_retry_after_s = day_retry_after_s

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _payload(self, messages: Sequence[dict], max_tokens: int) -> dict:
        p: dict = {
            "model": self.model,
            "messages": list(messages),
            "stream": True,
            "max_completion_tokens": int(max_tokens),
            "temperature": 0,
        }
        if self.stream_options:
            p["stream_options"] = {"include_usage": True}
        p.update(self.extra_params)
        return p

    async def chat(
        self,
        messages: Sequence[dict],
        *,
        max_tokens: int,
        timeout_s: float,
        first_token_timeout_s: Optional[float] = None,
    ) -> CallResult:
        st = _State(t0=time.monotonic())
        try:
            await asyncio.wait_for(
                self._run(messages, max_tokens, first_token_timeout_s, st), timeout=max(0.001, timeout_s)
            )
        except asyncio.TimeoutError:
            return self._fail(st, "timeout", f"no complete reply within {timeout_s:.2f}s")
        except _FirstTokenTimeout:
            return self._fail(st, "first_token_timeout", f"no content token within {first_token_timeout_s}s")
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            return self._fail(st, "connect", f"{type(e).__name__}: {e}")
        except Exception as e:   # transport errors, protocol errors, anything the transport raised
            kind = "midstream" if st.first_byte is not None else "connect"
            return self._fail(st, kind, f"{type(e).__name__}: {e}")
        return self._finish(st)

    # -- internals ---------------------------------------------------------------

    async def _run(self, messages: Sequence[dict], max_tokens: int, ft_timeout: Optional[float], st: _State) -> None:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        async with self._client.stream(
            "POST", f"{self.base_url}/chat/completions", json=self._payload(messages, max_tokens), headers=headers
        ) as resp:
            st.first_byte = time.monotonic()
            st.status = resp.status_code
            st.headers = {
                k.lower(): v for k, v in resp.headers.items()
                if k.lower().startswith("x-ratelimit-") or k.lower() == "retry-after"
            }
            if resp.status_code != 200:
                raw = (await resp.aread()).decode("utf-8", errors="replace")
                st.text = raw[:500]
                return
            lines = resp.aiter_lines().__aiter__()
            while True:
                timeout = None
                if st.first_token is None and ft_timeout is not None:
                    timeout = max(0.001, st.t0 + ft_timeout - time.monotonic())
                try:
                    line = await asyncio.wait_for(lines.__anext__(), timeout)
                except StopAsyncIteration:
                    return
                except asyncio.TimeoutError:
                    if st.first_token is None and ft_timeout is not None:
                        raise _FirstTokenTimeout()
                    raise
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    st.done = True
                    return
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                self._absorb(obj, st)

    @staticmethod
    def _absorb(obj: dict, st: _State) -> None:
        usage = obj.get("usage") or (obj.get("x_groq") or {}).get("usage")
        if isinstance(usage, dict):
            st.usage = usage
        for ch in obj.get("choices") or []:
            delta = ch.get("delta") or {}
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                if st.first_token is None:
                    st.first_token = time.monotonic()
                st.text += piece
            if ch.get("finish_reason"):
                st.finish_reason = ch["finish_reason"]

    def _ms(self, st: _State, t: Optional[float]) -> Optional[float]:
        return None if t is None else (t - st.t0) * 1000.0

    def _tokens(self, st: _State) -> tuple:
        if st.usage:
            return (
                int(st.usage.get("prompt_tokens") or 0),
                int(st.usage.get("completion_tokens") or 0),
                "provider",
            )
        return (0, approx_tokens(st.text), "estimated" if st.text else "none")

    @staticmethod
    def _reasoning(st: _State) -> Optional[int]:
        """usage.completion_tokens_details.reasoning_tokens when the provider reports it, else None."""
        d = (st.usage or {}).get("completion_tokens_details")
        if isinstance(d, dict) and isinstance(d.get("reasoning_tokens"), int):
            return int(d["reasoning_tokens"])
        return None

    def _finish(self, st: _State) -> CallResult:
        total = (time.monotonic() - st.t0) * 1000.0
        base = dict(
            status_code=st.status, headers=st.headers, first_byte_ms=self._ms(st, st.first_byte),
            ttft_ms=self._ms(st, st.first_token), total_ms=total, text=st.text, finish_reason=st.finish_reason,
            reasoning_tokens=self._reasoning(st),
        )
        if st.status != 200:
            return self._http_error(st, base)
        tin, tout, src = self._tokens(st)
        if not (st.done or st.finish_reason):
            return CallResult(ok=False, error_kind="midstream", message="stream ended before completion",
                              tokens_in=tin, tokens_out=tout, usage_source=src, **base)
        if not st.text:
            # Typically a reasoning model that spent its whole output budget thinking
            # (finish_reason "length"). The same call would fail the same way: not retryable.
            return CallResult(ok=False, error_kind="empty_reply",
                              message=f"reply contained no content (finish_reason={st.finish_reason})",
                              tokens_in=tin, tokens_out=tout, usage_source=src, **base)
        return CallResult(ok=True, tokens_in=tin, tokens_out=tout, usage_source=src, **base)

    def _http_error(self, st: _State, base: dict) -> CallResult:
        raw = base.pop("text", "")
        message = raw
        try:
            message = str(json.loads(raw).get("error", {}).get("message", raw))
        except (ValueError, AttributeError):
            pass
        code = st.status or 0
        ra = _to_float(st.headers.get("retry-after"))
        by = None
        if code == 429:
            kind, by = classify_429(st.headers, message, self.day_retry_after_s)
        elif code >= 500 or code in (424, 498):
            kind = "server_error"
        else:
            kind = "client_error"
        return CallResult(ok=False, error_kind=kind, message=message[:300], retry_after_s=ra,
                          classified_by=by, **base)

    def _fail(self, st: _State, kind: str, message: str) -> CallResult:
        total = (time.monotonic() - st.t0) * 1000.0
        tin, tout, src = self._tokens(st)
        return CallResult(
            ok=False, error_kind=kind, message=message, status_code=st.status, headers=st.headers,
            first_byte_ms=self._ms(st, st.first_byte), ttft_ms=self._ms(st, st.first_token),
            total_ms=total, text=st.text, tokens_in=tin, tokens_out=tout, usage_source=src,
        )
