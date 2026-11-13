"""Test helpers shared by gateway, pipeline and runner tests."""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from relay.llm_client import CallResult
from relay.tracing import EventLog, RequestContext


def ctx_for(log: EventLog, deadline_s: float = 5.0, rid: str = "r1", question: str = "How many layers?") -> RequestContext:
    now = time.monotonic()
    return RequestContext(request_id=rid, scenario="t", question_id="q", question=question,
                          arrive=now, deadline=now + deadline_s, log=log)


def ok_result(tin: int = 60, tout: int = 40, total_ms: float = 20.0, text: str = "Six layers [S1].",
              headers: Optional[dict] = None) -> CallResult:
    return CallResult(ok=True, text=text, status_code=200, tokens_in=tin, tokens_out=tout,
                      usage_source="provider", first_byte_ms=1.0, ttft_ms=5.0, total_ms=total_ms,
                      headers=headers or {}, finish_reason="stop")


def err_result(kind: str, status: Optional[int] = None, retry_after: Optional[float] = None,
               headers: Optional[dict] = None, message: str = "x") -> CallResult:
    return CallResult(ok=False, error_kind=kind, status_code=status, retry_after_s=retry_after,
                      message=message, total_ms=5.0, headers=headers or {})


class FakeClient:
    """Plays back scripted results. An item is a CallResult or a (delay_seconds, CallResult) pair."""

    def __init__(self, script) -> None:
        self.script = list(script)
        self.calls: list = []

    async def chat(self, messages, *, max_tokens, timeout_s, first_token_timeout_s=None):
        self.calls.append({"max_tokens": max_tokens, "timeout_s": timeout_s, "messages": messages})
        item = self.script.pop(0)
        if isinstance(item, tuple):
            await asyncio.sleep(item[0])
            item = item[1]
        return item


def events_named(log: EventLog, name: str) -> list:
    return [e for e in log.events if e["event"] == name]
