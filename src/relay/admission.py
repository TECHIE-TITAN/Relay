"""Door queue plus an in-flight cap (DESIGN.md 4.3, 5.1). The simple scheduling baseline.

  * One bounded queue. Served FIFO; newest-first once the head has waited longer than
    `newest_first_after_s` (a head that old has probably given up already).
  * At most `n_inflight` requests are inside the pipeline at once (a bulkhead), so
    requests cannot strand each other between stages.
  * Admission by estimate: a request is shed at once if (estimated wait + estimated
    service time) cannot fit before its deadline. The service estimate is an EWMA of how long
    slots were actually held, so retries and regenerations lengthen it and make admission
    stricter. The cap N itself is static here (adaptive N is not implemented).
  * At dequeue a request whose remaining time is below `min_service_s` is dropped.

Every wait is bounded by the request's own absolute deadline.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Optional

from .tracing import RequestContext


class Shed(Exception):
    """The request was not admitted (or was dropped while queued). `reason` is the finer cause."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(eq=False)
class _Waiter:
    ctx: RequestContext
    fut: "asyncio.Future"
    t_enq: float
    granted: bool = False


class Admission:
    def __init__(self, n_inflight: int, queue_cap: int, *, newest_first_after_s: float = 5.0,
                 min_service_s: float = 1.0, initial_service_s: float = 3.0, estimate_check: bool = True,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if n_inflight < 1 or queue_cap < 0:
            raise ValueError("n_inflight must be >= 1 and queue_cap >= 0")
        self.n = n_inflight
        self.queue_cap = queue_cap
        self.newest_first_after_s = newest_first_after_s
        self.min_service_s = min_service_s
        self.estimate_check = estimate_check
        self.clock = clock
        self.svc = initial_service_s
        self.inflight = 0
        self.peak_queue = 0
        self.peak_inflight = 0
        self.q: Deque[_Waiter] = deque()
        self.shed_counts: dict[str, int] = {}

    def _shed(self, reason: str) -> Shed:
        self.shed_counts[reason] = self.shed_counts.get(reason, 0) + 1
        return Shed(reason)

    def snapshot(self) -> dict:
        return {
            "queue_len": len(self.q), "inflight": self.inflight, "n_inflight": self.n,
            "peak_queue": self.peak_queue, "peak_inflight": self.peak_inflight,
            "service_estimate_s": round(self.svc, 3), "shed": dict(self.shed_counts),
        }

    def head_wait_s(self) -> float:
        return (self.clock() - self.q[0].t_enq) if self.q else 0.0

    def _take_slot(self) -> None:
        self.inflight += 1
        self.peak_inflight = max(self.peak_inflight, self.inflight)

    async def enter(self, ctx: RequestContext) -> float:
        """Wait for a slot. Returns seconds waited. Raises Shed. The caller must call release()."""
        t0 = self.clock()
        remaining = ctx.remaining()
        if remaining < self.min_service_s:
            raise self._shed("no_time_left")
        if self.inflight < self.n and not self.q:
            self._take_slot()
            return 0.0
        if len(self.q) >= self.queue_cap:
            raise self._shed("queue_full")
        if self.estimate_check:
            est_wait = (len(self.q) + 1) / self.n * self.svc
            if est_wait + self.svc > remaining:
                raise self._shed("infeasible")
        w = _Waiter(ctx, asyncio.get_running_loop().create_future(), t0)
        self.q.append(w)
        self.peak_queue = max(self.peak_queue, len(self.q))
        try:
            await asyncio.wait_for(w.fut, timeout=remaining)
        except asyncio.TimeoutError:
            self._abandon(w)
            raise self._shed("deadline_in_queue") from None
        except asyncio.CancelledError:
            self._abandon(w)
            raise
        return self.clock() - t0

    def _abandon(self, w: _Waiter) -> None:
        if w.granted:
            self.release(None)      # the slot was handed over but never used
        else:
            try:
                self.q.remove(w)
            except ValueError:
                pass

    def release(self, service_s: Optional[float]) -> None:
        self.inflight -= 1
        if service_s is not None and service_s > 0:
            self.svc = 0.8 * self.svc + 0.2 * service_s
        self._dispatch()

    def _dispatch(self) -> None:
        while self.inflight < self.n and self.q:
            head = self.q[0]
            stale = self.clock() - head.t_enq > self.newest_first_after_s
            w = self.q.pop() if stale else self.q.popleft()
            if w.fut.done():
                continue
            if w.ctx.remaining() < self.min_service_s:
                w.fut.set_exception(self._shed("expired_in_queue"))
                continue
            self._take_slot()
            w.granted = True
            w.fut.set_result(True)
