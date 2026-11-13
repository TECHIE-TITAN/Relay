"""The LLM gateway: every provider call goes through here (DESIGN.md sections 3.3, 4, 5.4).

What it does
  * Rate limiting in front of the provider: sliding 60 s windows for requests and for
    tokens (a log-based token bucket: never more than `limit` in any 60 s window, which
    a refill-rate bucket cannot guarantee). Reserve-then-reconcile: before sending it
    reserves prompt + max output tokens, afterwards it replaces the reservation with the
    real usage. Waiters are served strictly FIFO (one lock): a big call at the head makes
    the calls behind it wait. That is deliberate.
  * Deadline contract: the request's absolute deadline is read, never changed. A wait or a
    call that cannot fit before it ends the request now.
  * Retries are DISCRETIONARY calls: they stay in the request's slot, re-enter the same FIFO
    behind first attempts, draw on a capped share of the window (fail fast when it is
    empty), honour a global pause, are capped per call and per request, and never happen
    for a daily-limit 429.
  * Global pause after a 429 or when the circuit breaker opens: no dispatch (first attempts
    included) until it ends, plus jitter.
  * AIMD on 429: the permitted rate is cut multiplicatively and recovers linearly. The
    response header `x-ratelimit-limit-tokens` re-sizes the token window if it changes;
    `x-ratelimit-remaining-tokens` can only make us more conservative.
  * A daily ledger (persistent for real runs) with an artificial cap, so a run can never
    spend the last of the day's quota.

Deviations from the design text, on purpose
  * Sliding windows instead of refill-rate buckets (reason above).
  * A lone call always fits the discretionary share: at the documented 8K TPM a 15 % share
    (1,200 tokens) is smaller than one answer (~2.5K), so a strict share would forbid every retry.
    The share caps what comes AFTER that first discretionary call in a 60 s window.

Everything is configured by `GatewayConfig`; limits come from config/limits.yaml (DOCUMENTED)
or from a scenario (SIMULATED), never from literals here.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Optional, Sequence

from .llm_client import RETRYABLE, CallResult, LLMClient
from .textutil import approx_tokens
from .tracing import EventLog, RequestContext

BREAKER_KINDS = frozenset({"server_error", "timeout", "first_token_timeout", "connect", "midstream"})


@dataclass(frozen=True)
class GatewayConfig:
    rpm: int = 30                        # believed per-minute request limit
    tpm: int = 8000                      # believed per-minute token limit
    headroom: float = 0.9                # use at most this share of each limit
    limiter_enabled: bool = True         # False = the naive baseline (provider 429s are the control signal)
    discretionary_share: Optional[float] = 0.15   # None = retries are not capped
    max_attempts_per_request: int = 5
    max_retries_per_call: int = 2
    backoff_base_s: float = 0.5
    backoff_cap_s: float = 8.0
    honor_retry_after: bool = True
    call_timeout_s: float = 15.0
    first_token_timeout_s: Optional[float] = 8.0
    reserve_s: float = 0.3               # time kept back for the mandatory later steps (V0, release)
    initial_call_estimate_s: float = 3.0
    global_pause: bool = True
    pause_jitter_s: float = 0.5
    aimd_decrease: float = 0.7
    aimd_min: float = 0.2
    aimd_recover_per_s: float = 0.01
    breaker_threshold: int = 4
    breaker_cooldown_s: float = 3.0
    daily_token_cap: int = 160000        # artificial cap (80 % of a 200K day)
    daily_request_cap: int = 800
    # Optional hard cap on tokens spent by THIS gateway instance (a pilot). A model call is refused when
    # tokens already spent + its full reservation (prompt estimate + max_tokens) would pass it.
    pilot_token_cap: Optional[int] = None


class LimiterTimeout(Exception):
    """Waiting for quota would not fit before the deadline."""


class BudgetDenied(Exception):
    """The discretionary share is used up."""


class TooLarge(Exception):
    """One call alone exceeds the whole window."""


class _Window:
    """Amounts recorded in the last `span` seconds, with a soft limit that can shrink."""

    def __init__(self, limit: float, hard: float = math.inf, span: float = 60.0) -> None:
        self.limit = limit
        self.hard = hard
        self.span = span
        self.items: Deque[list] = deque()

    def _trim(self, now: float) -> None:
        while self.items and now - self.items[0][0] >= self.span:
            self.items.popleft()

    def used(self, now: float) -> float:
        self._trim(now)
        return float(sum(i[1] for i in self.items))

    def add(self, now: float, amount: float) -> list:
        e = [now, float(amount)]
        self.items.append(e)
        return e

    def wait_for(self, amount: float, now: float) -> float:
        """Seconds until `amount` fits. A lone call (empty window) always fits unless above `hard`."""
        if amount > self.hard:
            return math.inf
        used = self.used(now)
        if used == 0.0 or used + amount <= self.limit:
            return 0.0
        freed = 0.0
        for t, a in self.items:
            freed += a
            if used - freed + amount <= self.limit or used - freed == 0.0:
                return max(0.0, t + self.span - now)
        return self.span


class DailyLedger:
    """Client-side count of THIS tool's calls today (UTC date: an assumption, the reset time is not documented).

    It cannot see usage by anything else on the account, so its cap is a safety margin, not the
    provider's number. For real runs it persists to a JSON file so a restart cannot forget.
    """

    def __init__(self, token_cap: int, request_cap: int, path: Optional[Path] = None,
                 today: Optional[str] = None) -> None:
        self.token_cap = token_cap
        self.request_cap = request_cap
        self.path = path
        self.today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.tokens = 0
        self.requests = 0
        self.provider_daily_limit_hit = False
        if path is not None and path.is_file():
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
                if d.get("date") == self.today:
                    self.tokens = int(d.get("tokens", 0))
                    self.requests = int(d.get("requests", 0))
                    self.provider_daily_limit_hit = bool(d.get("provider_daily_limit_hit", False))
            except (ValueError, OSError):
                pass

    def record(self, tokens: int) -> None:
        self.requests += 1
        self.tokens += max(0, int(tokens))
        self._save()

    def mark_provider_limit(self) -> None:
        self.provider_daily_limit_hit = True
        self._save()

    @property
    def exhausted(self) -> bool:
        return self.provider_daily_limit_hit or self.tokens >= self.token_cap or self.requests >= self.request_cap

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "date": self.today, "tokens": self.tokens, "requests": self.requests,
            "token_cap": self.token_cap, "request_cap": self.request_cap,
            "provider_daily_limit_hit": self.provider_daily_limit_hit,
            "note": "client-side count of this tool's own calls; the provider's own usage may be higher",
        }, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


@dataclass
class CallOutcome:
    ok: bool
    result: Optional[CallResult] = None
    failure: Optional[str] = None       # why the call did not succeed (see status_for)
    last_error_kind: Optional[str] = None
    attempts: int = 0                   # provider attempts made by this call
    retries: int = 0

    @property
    def status(self) -> str:
        return "ok" if self.ok else status_for(self.failure, self.last_error_kind)


def status_for(failure: Optional[str], last_error_kind: Optional[str]) -> str:
    """Map a gateway failure to the brief's status enum. The finer reason stays in the events."""
    if failure == "deadline":                      # no time left to even try
        return "timeout"
    if failure in ("limiter_timeout", "budget_denied", "daily_exhausted", "too_large", "pilot_cap", "ledger_cap"):
        return "rate_limited"
    kind = last_error_kind or ""
    if kind.startswith("rate_limit"):
        return "rate_limited"
    if kind in ("timeout", "first_token_timeout"):
        return "timeout"
    return "error"


@dataclass
class _Reservation:
    tokens_entry: list
    disc_tokens_entry: Optional[list] = None


class Gateway:
    def __init__(self, client: LLMClient, cfg: GatewayConfig, log: EventLog, *,
                 clock: Callable[[], float] = time.monotonic, ledger: Optional[DailyLedger] = None,
                 rng: Optional[random.Random] = None) -> None:
        self.client = client
        self.cfg = cfg
        self.log = log
        self.clock = clock
        self.ledger = ledger or DailyLedger(cfg.daily_token_cap, cfg.daily_request_cap)
        self.pilot_spent = 0                # tokens this gateway has actually spent (for pilot_token_cap)
        self._held = 0                      # full reservations of calls admitted but not yet finished
        self.cap_refused: Optional[str] = None   # "pilot" / "daily_ledger" once a call was refused by a token cap
        self.rng = rng or random.Random(0)
        self.base_rpm = float(cfg.rpm)
        self.base_tpm = float(cfg.tpm)
        self.rpm_win = _Window(0.0)
        self.tpm_win = _Window(0.0)
        self.d_rpm = _Window(0.0)
        self.d_tpm = _Window(0.0)
        self._f0 = 1.0
        self._t429 = clock()
        self._applied = -1.0
        self.paused_until = 0.0
        self._breaker_failures = 0
        self._call_est = cfg.initial_call_estimate_s
        self._lock: Optional[asyncio.Lock] = None   # created lazily inside the running loop
        self.calls_in_flight = 0
        self._apply_aimd(clock())

    # -- limits, AIMD ----------------------------------------------------------------

    def _factor(self, now: float) -> float:
        return min(1.0, self._f0 + (now - self._t429) * self.cfg.aimd_recover_per_s)

    def _set_limits(self, f: float) -> None:
        c = self.cfg
        self.rpm_win.limit = max(1.0, self.base_rpm * c.headroom * f)
        self.tpm_win.limit = max(1.0, self.base_tpm * c.headroom * f)
        self.rpm_win.hard = self.base_rpm * c.headroom
        self.tpm_win.hard = self.base_tpm * c.headroom
        share = c.discretionary_share
        if share is not None:
            self.d_rpm.limit = self.rpm_win.limit * share
            self.d_tpm.limit = self.tpm_win.limit * share
        self._applied = f

    def _apply_aimd(self, now: float) -> None:
        f = self._factor(now)
        if abs(f - self._applied) > 0.005:
            self._set_limits(f)

    def _pause(self, seconds: float, reason: str) -> None:
        if not self.cfg.global_pause:
            return
        now = self.clock()
        until = now + max(0.0, seconds) + self.rng.random() * self.cfg.pause_jitter_s
        if until > self.paused_until:
            self.paused_until = until
            self.log.emit(None, "gateway", "pause_start", reason=reason,
                          until_mono=self.log.rel(until), seconds=round(until - now, 3))

    def _on_rate_limited(self) -> None:
        now = self.clock()
        eff = self._factor(now)
        self._f0 = max(self.cfg.aimd_min, eff * self.cfg.aimd_decrease)
        self._t429 = now
        self._apply_aimd(now)

    # -- header synchronisation ---------------------------------------------------------

    def _sync_headers(self, ctx: RequestContext, headers: dict) -> None:
        if not headers:
            return
        ctx.emit("ratelimit_headers", headers=headers)
        now = self.clock()
        lim = _to_int(headers.get("x-ratelimit-limit-tokens"))
        if lim and lim != int(self.base_tpm):
            self.log.emit(None, "gateway", "limit_changed", what="tpm", old=int(self.base_tpm), new=lim,
                          source="response header x-ratelimit-limit-tokens")
            self.base_tpm = float(lim)
            self._set_limits(self._factor(now))
        rem = _to_int(headers.get("x-ratelimit-remaining-tokens"))
        if rem is not None:
            gap = (self.base_tpm - self.tpm_win.used(now)) - rem
            if gap > 0.05 * self.base_tpm:
                self.tpm_win.add(now, gap)      # the provider has seen more usage than we have counted
                ctx.emit("header_sync", tokens_gap=round(gap))

    # -- observability ---------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        return {
            "rpm_used": self.rpm_win.used(now), "rpm_limit": round(self.rpm_win.limit, 1),
            "tpm_used": self.tpm_win.used(now), "tpm_limit": round(self.tpm_win.limit, 1),
            "aimd_factor": round(self._factor(now), 3),
            "paused": now < self.paused_until,
            "daily_tokens": self.ledger.tokens, "daily_requests": self.ledger.requests,
            "daily_token_cap": self.ledger.token_cap, "daily_exhausted": self.ledger.exhausted,
            "calls_in_flight": self.calls_in_flight, "call_estimate_s": round(self._call_est, 3),
        }

    def pressure(self) -> dict[str, Any]:
        """Fractions of each budget in use. The degradation ladder is derived from these by the pipeline."""
        now = self.clock()
        return {
            "rpm": self.rpm_win.used(now) / max(1.0, self.base_rpm * self.cfg.headroom),
            "tpm": self.tpm_win.used(now) / max(1.0, self.base_tpm * self.cfg.headroom),
            "daily": self.ledger.tokens / max(1, self.ledger.token_cap),
            "paused": now < self.paused_until,
            "exhausted": self.ledger.exhausted,
        }

    @property
    def call_estimate_s(self) -> float:
        return self._call_est

    # -- reservation -------------------------------------------------------------------------

    def _disc_wait(self, est_tokens: float, now: float) -> float:
        if self.cfg.discretionary_share is None:
            return 0.0
        return max(self.d_rpm.wait_for(1, now), self.d_tpm.wait_for(est_tokens, now))

    async def _reserve(self, ctx: RequestContext, est_tokens: int, discretionary: bool) -> _Reservation:
        cfg = self.cfg
        afford = lambda: ctx.remaining() - cfg.reserve_s - self._call_est   # noqa: E731
        if discretionary and cfg.limiter_enabled and self._disc_wait(est_tokens, self.clock()) > 0:
            raise BudgetDenied()
        if self._lock is None:
            self._lock = asyncio.Lock()
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=max(0.001, afford()))
        except asyncio.TimeoutError:
            raise LimiterTimeout() from None
        try:
            t_wait = None
            while True:
                now = self.clock()
                if cfg.limiter_enabled:
                    self._apply_aimd(now)
                    waits = [self.rpm_win.wait_for(1, now), self.tpm_win.wait_for(est_tokens, now)]
                    if cfg.global_pause:
                        waits.append(max(0.0, self.paused_until - now))
                    wait = max(waits)
                else:
                    wait = 0.0
                if wait == math.inf:
                    raise TooLarge()
                if wait <= 0.0:
                    break
                if wait > afford():
                    raise LimiterTimeout()
                if t_wait is None:
                    t_wait = now
                    ctx.emit("limiter_wait_start", wait_ms=round(wait * 1000))
                await asyncio.sleep(wait)
            now = self.clock()
            if discretionary and cfg.limiter_enabled and self._disc_wait(est_tokens, now) > 0:
                raise BudgetDenied()
            self.rpm_win.add(now, 1)
            res = _Reservation(tokens_entry=self.tpm_win.add(now, est_tokens))
            if discretionary and cfg.discretionary_share is not None:
                self.d_rpm.add(now, 1)
                res.disc_tokens_entry = self.d_tpm.add(now, est_tokens)
            if t_wait is not None:
                ctx.emit("limiter_wait_end", waited_ms=round((now - t_wait) * 1000))
            return res
        finally:
            self._lock.release()

    # -- the call ------------------------------------------------------------------------------

    async def call(self, ctx: RequestContext, messages: Sequence[dict], *, max_tokens: int, purpose: str,
                   discretionary: bool = False) -> CallOutcome:
        cfg = self.cfg
        est_prompt = approx_tokens(json.dumps(list(messages)))
        reserved = est_prompt + max_tokens
        retries_here = 0
        attempts_here = 0
        last_kind: Optional[str] = None

        def fail(why: str) -> CallOutcome:
            ctx.emit("call_failed", purpose=purpose, failure=why, last_error=last_kind)
            return CallOutcome(ok=False, failure=why, last_error_kind=last_kind,
                               attempts=attempts_here, retries=retries_here)

        while True:
            if self.ledger.exhausted:
                return fail("daily_exhausted")
            if ctx.attempts >= cfg.max_attempts_per_request:
                return fail("attempt_cap")
            if ctx.remaining() < cfg.reserve_s + min(self._call_est, 1.0):
                return fail("deadline")
            # Cap check and hold, as ONE synchronous step (no await between them, so no other request can
            # interleave): spent + reservations still outstanding for in-flight calls + this call's full
            # reservation must fit under both caps. The provider charges max_tokens up front.
            held = self._held
            if cfg.pilot_token_cap is not None and self.pilot_spent + held + reserved > cfg.pilot_token_cap:
                ctx.emit("cap_check", cap="pilot", spent=self.pilot_spent, outstanding=held, reserved=reserved,
                         limit=cfg.pilot_token_cap)
                self.cap_refused = self.cap_refused or "pilot"
                return fail("pilot_cap")
            if self.ledger.tokens + held + reserved > self.ledger.token_cap:
                ctx.emit("cap_check", cap="daily_ledger", spent=self.ledger.tokens, outstanding=held,
                         reserved=reserved, limit=self.ledger.token_cap)
                self.cap_refused = self.cap_refused or "daily_ledger"
                return fail("ledger_cap")
            self._held += reserved
            disc = discretionary or retries_here > 0
            try:
                res = await self._reserve(ctx, reserved, disc)
            except LimiterTimeout:
                self._held -= reserved
                return fail("limiter_timeout")
            except BudgetDenied:
                self._held -= reserved
                return fail("budget_denied")
            except TooLarge:
                self._held -= reserved
                return fail("too_large")
            except BaseException:
                self._held -= reserved            # e.g. cancellation while waiting for the limiter
                raise

            ctx.attempts += 1
            attempts_here += 1
            if disc:
                ctx.discretionary_calls += 1
            if retries_here > 0:
                ctx.retries += 1
            timeout_s = min(cfg.call_timeout_s, ctx.remaining() - cfg.reserve_s)
            ft = cfg.first_token_timeout_s
            if ft is not None:
                ft = min(ft, timeout_s)
            ctx.emit("send", attempt=ctx.attempts, purpose=purpose, discretionary=disc,
                     reserved_tokens=reserved, timeout_s=round(timeout_s, 3))
            self.calls_in_flight += 1
            try:
                r = await self.client.chat(messages, max_tokens=max_tokens, timeout_s=timeout_s,
                                           first_token_timeout_s=ft)
            finally:
                self.calls_in_flight -= 1
                self._held -= reserved            # released here; the actual spend is recorded just below, with no await between

            tin = r.tokens_in if r.usage_source == "provider" else est_prompt
            tout = r.tokens_out
            actual = tin + tout
            # Reconcile the reservation. Calls that never reached the model, and 5xx (documented as
            # not billed), refund fully. Everything else keeps at least the prompt, conservatively.
            if r.error_kind in ("connect", "server_error"):
                res.tokens_entry[1] = 0.0
                if res.disc_tokens_entry is not None:
                    res.disc_tokens_entry[1] = 0.0
            else:
                res.tokens_entry[1] = float(actual if r.ok else max(actual, est_prompt))
                if res.disc_tokens_entry is not None:
                    res.disc_tokens_entry[1] = res.tokens_entry[1]
            rejected = (r.error_kind or "").startswith("rate_limit")
            if r.error_kind in ("connect", "server_error"):
                pass                                  # never reached the model / documented as not billed
            elif rejected:
                self.ledger.record(0)                 # counts as a request, no tokens
            else:
                amount = actual if r.ok else max(actual, est_prompt)
                self.ledger.record(amount)
                self.pilot_spent += amount
            if r.ok or r.usage_source == "provider":
                ctx.tokens_in += tin
                ctx.tokens_out += tout
            elif r.error_kind not in ("connect", "server_error") and not rejected:
                ctx.tokens_unaccounted_est += max(actual, est_prompt)
            ctx.emit("call_end", ok=r.ok, error_kind=r.error_kind, status_code=r.status_code,
                     duration_ms=round(r.total_ms, 1),
                     ttft_ms=None if r.ttft_ms is None else round(r.ttft_ms, 1),
                     first_byte_ms=None if r.first_byte_ms is None else round(r.first_byte_ms, 1),
                     tokens_in=tin, tokens_out=tout, usage_source=r.usage_source,
                     reasoning_tokens=r.reasoning_tokens, max_tokens=max_tokens, est_prompt_tokens=est_prompt,
                     finish_reason=r.finish_reason, retry_after_s=r.retry_after_s, classified_by=r.classified_by, message=r.message[:160],
                     purpose=purpose, discretionary=disc)
            self._sync_headers(ctx, r.headers)

            if r.ok:
                self._breaker_failures = 0
                self._call_est = 0.8 * self._call_est + 0.2 * (r.total_ms / 1000.0)
                return CallOutcome(ok=True, result=r, attempts=attempts_here, retries=retries_here)

            kind = r.error_kind or "error"
            last_kind = kind
            ctx.last_error = kind
            if kind == "rate_limit_day":
                self.ledger.mark_provider_limit()
                ctx.emit("daily_limit", classified_by=r.classified_by, retry_after_s=r.retry_after_s)
                return fail("daily_exhausted")
            if kind == "rate_limit_minute":
                self._on_rate_limited()
                self._pause(r.retry_after_s if r.retry_after_s is not None else 1.0, "429")
            elif kind in BREAKER_KINDS:
                self._breaker_failures += 1
                if self._breaker_failures >= cfg.breaker_threshold:
                    self._breaker_failures = 0
                    ctx.emit("breaker_open", cooldown_s=cfg.breaker_cooldown_s)
                    self._pause(cfg.breaker_cooldown_s, "breaker_open")
            if kind not in RETRYABLE:
                return fail(kind)
            if retries_here >= cfg.max_retries_per_call:
                return fail("retries_exhausted")
            backoff = self.rng.random() * min(cfg.backoff_cap_s, cfg.backoff_base_s * (2 ** retries_here))
            if kind == "rate_limit_minute" and cfg.honor_retry_after and r.retry_after_s is not None:
                backoff = max(backoff, r.retry_after_s)
            if ctx.remaining() < backoff + cfg.reserve_s + min(self._call_est, 1.0):
                return fail("deadline_no_retry")
            ctx.emit("retry_sleep_start", sleep_ms=round(backoff * 1000), after=kind)
            await asyncio.sleep(backoff)
            ctx.emit("retry_sleep_end")
            retries_here += 1


def _to_int(v: Any) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
