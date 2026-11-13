"""Event log and per-request context.

Every event is one JSON line: {t_mono, request_id, stage, event, remaining_ms, ...}.
`t_mono` is seconds since the run started, on the monotonic clock. Lines are
written whole and flushed, so a reader tailing the file (the dashboard) sees only
complete lines. Nothing here depends on the dashboard.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


class EventLog:
    def __init__(self, path: Optional[Path] = None, clock: Callable[[], float] = time.monotonic,
                 keep_in_memory: bool = True) -> None:
        self.clock = clock
        self.t0 = clock()
        self.events: list[dict[str, Any]] = []
        self._keep = keep_in_memory
        self._f = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._f = open(path, "a", encoding="utf-8")

    def rel(self, t: float) -> float:
        """A monotonic timestamp expressed as seconds since the run started."""
        return round(t - self.t0, 6)

    def emit(self, request_id: Optional[str], stage: str, event: str,
             remaining_ms: Optional[float] = None, **fields: Any) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "t_mono": self.rel(self.clock()), "request_id": request_id, "stage": stage, "event": event,
        }
        if remaining_ms is not None:
            rec["remaining_ms"] = remaining_ms
        rec.update(fields)
        if self._keep:
            self.events.append(rec)
        if self._f is not None:
            self._f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
            self._f.flush()
        return rec

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None


@dataclass
class RequestContext:
    """State of one request. `deadline` is absolute, set once at arrival, never extended."""

    request_id: str
    scenario: str
    question_id: str
    question: str
    arrive: float
    deadline: float
    log: EventLog
    clock: Callable[[], float] = time.monotonic
    setting: str = ""
    stage: str = "arrive"
    attempts: int = 0               # provider attempts so far
    retries: int = 0                # attempts that were retries of a failed call
    discretionary_calls: int = 0    # retries, regenerations, reformulations
    tokens_in: int = 0              # from provider usage (or labelled estimates), successful or not
    tokens_out: int = 0
    tokens_unaccounted_est: int = 0  # estimate for failed calls that returned no usage (NOT in tokens_in/out)
    last_error: Optional[str] = None
    release_at: Optional[float] = None   # when the user-visible answer was released
    notes: dict = field(default_factory=dict)

    def remaining(self) -> float:
        return self.deadline - self.clock()

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        return self.log.emit(
            self.request_id, self.stage, event, remaining_ms=round(self.remaining() * 1000.0, 1), **fields
        )
