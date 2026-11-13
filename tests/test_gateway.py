from __future__ import annotations

import asyncio
import math
import time

from helpers import FakeClient, ctx_for, err_result, events_named, ok_result

from relay.gateway import (
    CallOutcome, DailyLedger, Gateway, GatewayConfig, _Window, status_for,
)
from relay.tracing import EventLog

MSGS = [{"role": "user", "content": "hello " * 20}]


def cfg(**kw) -> GatewayConfig:
    base = dict(rpm=1000, tpm=100000, headroom=1.0, backoff_base_s=0.01, backoff_cap_s=0.05,
                pause_jitter_s=0.0, reserve_s=0.05, initial_call_estimate_s=0.05, call_timeout_s=1.0,
                first_token_timeout_s=None, breaker_threshold=99)
    base.update(kw)
    return GatewayConfig(**base)


def run_call(client, config, *, ctx_kw=None, n=1, ledger=None, ctx_setup=None):
    async def go():
        log = EventLog(None)
        gw = Gateway(client, config, log, ledger=ledger)
        outs, ctxs = [], []
        for i in range(n):
            ctx = ctx_for(log, **(ctx_kw or {}))
            if ctx_setup:
                ctx_setup(ctx)
            outs.append(await gw.call(ctx, MSGS, max_tokens=50, purpose="draft"))
            ctxs.append(ctx)
        return gw, log, outs, ctxs
    return asyncio.run(go())


def test_success_records_provider_usage_and_reconciles_reservation():
    client = FakeClient([ok_result(tin=60, tout=40)])
    gw, log, (out,), (ctx,) = run_call(client, cfg())
    assert out.ok and out.attempts == 1 and out.retries == 0 and out.status == "ok"
    assert (ctx.tokens_in, ctx.tokens_out, ctx.attempts, ctx.retries) == (60, 40, 1, 0)
    # the reservation (prompt estimate + 50) was replaced by actual usage
    assert gw.tpm_win.used(time.monotonic()) == 100
    assert gw.ledger.tokens == 100 and gw.ledger.requests == 1
    send = events_named(log, "send")[0]
    assert send["discretionary"] is False and send["reserved_tokens"] > 50 and "remaining_ms" in send
    assert events_named(log, "call_end")[0]["usage_source"] == "provider"


def test_retry_after_5xx_is_discretionary_and_counted():
    client = FakeClient([err_result("server_error", 503), ok_result()])
    gw, log, (out,), (ctx,) = run_call(client, cfg())
    assert out.ok and out.attempts == 2 and out.retries == 1
    assert ctx.retries == 1 and ctx.discretionary_calls == 1 and ctx.attempts == 2
    assert [e["discretionary"] for e in events_named(log, "send")] == [False, True]
    assert len(events_named(log, "retry_sleep_start")) == 1
    # a 5xx is documented as not billed: only the successful call is in the ledger
    assert gw.ledger.tokens == 100


def test_daily_429_is_never_retried_and_blocks_later_calls():
    client = FakeClient([err_result("rate_limit_day", 429, retry_after=7200)])
    gw, log, outs, ctxs = run_call(client, cfg(), n=2)
    assert not outs[0].ok and outs[0].failure == "daily_exhausted" and outs[0].status == "rate_limited"
    assert outs[0].attempts == 1
    assert outs[1].failure == "daily_exhausted"
    assert len(client.calls) == 1                 # the second request never reached the client
    assert gw.ledger.exhausted and events_named(log, "daily_limit")


def test_minute_429_pauses_dispatch_and_cuts_rate_then_retry_succeeds():
    client = FakeClient([err_result("rate_limit_minute", 429, retry_after=0.25), ok_result()])
    gw, log, (out,), _ = run_call(client, cfg())
    assert out.ok and out.retries == 1
    pause = events_named(log, "pause_start")[0]
    sends = events_named(log, "send")
    assert sends[1]["t_mono"] >= pause["until_mono"] - 1e-3        # no dispatch while paused (invariant I6)
    assert gw.snapshot()["aimd_factor"] < 1.0
    assert sends[1]["t_mono"] - sends[0]["t_mono"] >= 0.24


def test_retries_per_call_are_capped():
    client = FakeClient([err_result("server_error", 503)] * 3 + [ok_result()])
    _, log, (out,), (ctx,) = run_call(client, cfg(max_retries_per_call=2))
    assert not out.ok and out.failure == "retries_exhausted" and out.attempts == 3
    assert out.status == "error" and len(client.calls) == 3


def test_attempts_per_request_are_capped():
    client = FakeClient([ok_result()])
    _, _, (out,), _ = run_call(client, cfg(max_attempts_per_request=5), ctx_setup=lambda c: setattr(c, "attempts", 5))
    assert out.failure == "attempt_cap" and len(client.calls) == 0


def test_no_call_when_deadline_is_too_close():
    client = FakeClient([ok_result()])
    _, _, (out,), _ = run_call(client, cfg(), ctx_kw={"deadline_s": 0.04})
    assert out.failure == "deadline" and out.status == "timeout" and not client.calls


def test_retry_is_skipped_when_backoff_cannot_fit_before_the_deadline():
    client = FakeClient([err_result("rate_limit_minute", 429, retry_after=30.0), ok_result()])
    _, log, (out,), _ = run_call(client, cfg(), ctx_kw={"deadline_s": 2.0})
    assert out.failure == "deadline_no_retry" and out.status == "rate_limited"     # the cause was the 429
    assert len(client.calls) == 1 and not events_named(log, "retry_sleep_start")


def test_limiter_fails_fast_instead_of_waiting_past_the_deadline():
    client = FakeClient([ok_result(), ok_result()])
    gw, log, outs, _ = run_call(client, cfg(rpm=1), n=2, ctx_kw={"deadline_s": 2.0})
    assert outs[0].ok
    assert outs[1].failure == "limiter_timeout" and outs[1].status == "rate_limited"
    assert len(client.calls) == 1


def test_discretionary_share_is_capped_and_retry_fails_fast_when_empty():
    # tpm 1000, share 15% => 150 tokens of discretionary traffic per minute; a lone call is always allowed.
    config = cfg(rpm=1000, tpm=1000, discretionary_share=0.15)
    client = FakeClient([
        err_result("server_error", 503), ok_result(tin=60, tout=40),     # request 1: retry allowed (lone call)
        err_result("server_error", 503), ok_result(),                    # request 2: retry denied
    ])
    gw, log, outs, _ = run_call(client, config, n=2)
    assert outs[0].ok and outs[0].retries == 1
    assert not outs[1].ok and outs[1].failure == "budget_denied" and outs[1].status == "rate_limited"
    assert len(client.calls) == 3


def _discretionary_calls(client, config, n, *, ctx_kw=None, ledger=None):
    async def go():
        log = EventLog(None)
        gw = Gateway(client, config, log, ledger=ledger)
        outs = []
        for i in range(n):
            ctx = ctx_for(log, rid=f"r{i}", **(ctx_kw or {}))
            outs.append(await gw.call(ctx, MSGS, max_tokens=50, purpose="regenerate", discretionary=True))
        return gw, log, outs
    return asyncio.run(go())


def test_null_discretionary_share_removes_only_the_retry_throttle():
    # with a tiny share the second discretionary call in the window is denied ...
    client = FakeClient([ok_result(), ok_result()])
    _, _, outs = _discretionary_calls(client, cfg(discretionary_share=0.001), 2)
    assert outs[0].ok and outs[1].failure == "budget_denied" and len(client.calls) == 1
    # ... and with share None both go through
    client = FakeClient([ok_result(), ok_result()])
    _, _, outs = _discretionary_calls(client, cfg(discretionary_share=None), 2)
    assert all(o.ok for o in outs) and len(client.calls) == 2


def test_null_discretionary_share_still_enforces_the_request_and_token_windows():
    client = FakeClient([ok_result(), ok_result()])
    _, _, outs = _discretionary_calls(client, cfg(discretionary_share=None, rpm=1), 2, ctx_kw={"deadline_s": 2.0})
    assert outs[0].ok and outs[1].failure == "limiter_timeout" and len(client.calls) == 1      # RPM window
    client = FakeClient([ok_result(), ok_result(), ok_result()])
    _, _, outs = _discretionary_calls(client, cfg(discretionary_share=None, tpm=200), 3, ctx_kw={"deadline_s": 2.0})
    assert [o.ok for o in outs] == [True, True, False] and outs[2].failure == "limiter_timeout"   # TPM window
    assert len(client.calls) == 2


def test_null_discretionary_share_still_enforces_retry_caps_and_never_retries_a_daily_429():
    client = FakeClient([err_result("server_error", 503)] * 3 + [ok_result()])
    _, _, (out,) = _discretionary_calls(client, cfg(discretionary_share=None, max_retries_per_call=2), 1)
    assert out.failure == "retries_exhausted" and out.attempts == 3 and len(client.calls) == 3
    client = FakeClient([err_result("rate_limit_day", 429, retry_after=7200), ok_result()])
    gw, _, (out,) = _discretionary_calls(client, cfg(discretionary_share=None), 1)
    assert out.failure == "daily_exhausted" and out.attempts == 1 and len(client.calls) == 1 and gw.ledger.exhausted


def test_null_discretionary_share_still_enforces_the_pilot_cap_the_ledger_cap_and_attempt_cap():
    client = FakeClient([ok_result(), ok_result()])
    gw, _, outs = _discretionary_calls(client, cfg(discretionary_share=None, pilot_token_cap=150), 2)
    assert outs[0].ok and outs[1].failure == "pilot_cap" and gw.cap_refused == "pilot" and len(client.calls) == 1
    ledger = DailyLedger(token_cap=100, request_cap=100)
    ledger.tokens = 50
    client = FakeClient([ok_result()])
    gw, _, (out,) = _discretionary_calls(client, cfg(discretionary_share=None), 1, ledger=ledger)
    assert out.failure == "ledger_cap" and gw.cap_refused == "daily_ledger" and not client.calls
    client = FakeClient([ok_result()])
    async def go():
        log = EventLog(None)
        ctx = ctx_for(log)
        ctx.attempts = 5
        return await Gateway(client, cfg(discretionary_share=None, max_attempts_per_request=5), log).call(
            ctx, MSGS, max_tokens=50, purpose="regenerate", discretionary=True)
    assert asyncio.run(go()).failure == "attempt_cap" and not client.calls


def test_naive_policy_has_no_limiter_pause_or_share():
    client = FakeClient([err_result("rate_limit_minute", 429, retry_after=0.2), ok_result()])
    config = cfg(limiter_enabled=False, global_pause=False, discretionary_share=None, honor_retry_after=False)
    gw, log, (out,), _ = run_call(client, config)
    assert out.ok and not events_named(log, "pause_start")


def test_circuit_breaker_opens_after_consecutive_failures():
    client = FakeClient([err_result("server_error", 503), err_result("server_error", 503), ok_result()])
    _, log, (out,), _ = run_call(client, cfg(breaker_threshold=2, breaker_cooldown_s=0.05, max_retries_per_call=2))
    assert out.ok
    assert events_named(log, "breaker_open")
    assert any(e["reason"] == "breaker_open" for e in events_named(log, "pause_start"))


def test_client_error_is_not_retried():
    client = FakeClient([err_result("client_error", 400), ok_result()])
    _, _, (out,), _ = run_call(client, cfg())
    assert out.failure == "client_error" and out.status == "error" and len(client.calls) == 1


def test_header_sync_resizes_token_window_and_adds_missing_usage():
    hdr = {"x-ratelimit-limit-tokens": "4000", "x-ratelimit-remaining-tokens": "100"}
    client = FakeClient([ok_result(tin=10, tout=10, headers=hdr)])
    gw, log, (out,), _ = run_call(client, cfg(tpm=8000))
    assert gw.base_tpm == 4000 and gw.tpm_win.limit == 4000
    changed = events_named(log, "limit_changed")[0]
    assert changed["old"] == 8000 and changed["new"] == 4000
    # the provider says only 100 tokens remain although we counted 20: the window absorbs the gap
    assert gw.tpm_win.used(time.monotonic()) >= 3800
    assert events_named(log, "ratelimit_headers")


def test_real_429_shape_without_token_headers_leaves_the_window_alone_and_still_retries():
    # Shape observed from the real service (results/probe_real.jsonl): a request-limit 429 carries
    # retry-after and the request-limit headers only, with no x-ratelimit-*-tokens.
    hdr = {"retry-after": "2", "x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "938",
           "x-ratelimit-reset-requests": "1h29m16.8s"}
    client = FakeClient([err_result("rate_limit_minute", 429, retry_after=0.1, headers=hdr), ok_result()])
    gw, log, (out,), _ = run_call(client, cfg(tpm=8000))
    assert out.ok and out.retries == 1
    assert gw.base_tpm == 8000 and not events_named(log, "limit_changed")
    assert not events_named(log, "header_sync")


def test_pilot_cap_checks_spent_plus_full_reservation_before_every_call():
    client = FakeClient([ok_result(tin=60, tout=40), ok_result(tin=60, tout=40), ok_result(tin=60, tout=40)])
    gw, log, outs, _ = run_call(client, cfg(pilot_token_cap=250), n=3)
    # reservation per call is the prompt estimate + max_tokens 50 (about 90); each call actually used 100
    assert [o.ok for o in outs] == [True, True, False]
    assert outs[2].failure == "pilot_cap" and len(client.calls) == 2         # the third call was never sent
    assert gw.pilot_spent == 200
    chk = events_named(log, "cap_check")[0]
    assert chk["cap"] == "pilot" and chk["spent"] == 200 and chk["spent"] + chk["reserved"] > 250


def _concurrent_cap_run(*, pilot_cap=None, ledger=None, n=4):
    """n requests start together, each call takes 0.2 s, so every reservation is outstanding at once."""
    import json as _json
    from relay.textutil import approx_tokens
    reserved = approx_tokens(_json.dumps(MSGS)) + 50                     # what one call reserves
    client = FakeClient([(0.2, ok_result(tin=60, tout=40))] * n)

    async def go():
        log = EventLog(None)
        gw = Gateway(client, cfg(pilot_token_cap=pilot_cap(reserved) if pilot_cap else None), log,
                     ledger=ledger(reserved) if ledger else None)
        ctxs = [ctx_for(log, rid=f"r{i}") for i in range(n)]
        outs = await asyncio.gather(*[gw.call(c, MSGS, max_tokens=50, purpose="draft") for c in ctxs])
        return gw, log, outs
    gw, log, outs = asyncio.run(go())
    return gw, log, outs, client, reserved


def test_concurrent_calls_cannot_jointly_exceed_the_pilot_cap():
    # room for two full reservations (2.5 x one call), but four requests arrive at once
    gw, log, outs, client, reserved = _concurrent_cap_run(pilot_cap=lambda r: int(2.5 * r))
    assert sorted(o.ok for o in outs) == [False, False, True, True]
    assert len(client.calls) == 2                                          # the other two were never sent
    assert all(o.failure == "pilot_cap" for o in outs if not o.ok)
    assert gw._held == 0                                                   # every hold was released
    refused = [e for e in events_named(log, "cap_check") if e["cap"] == "pilot"]
    assert len(refused) == 2 and all(e["outstanding"] == 2 * reserved for e in refused)


def test_concurrent_calls_cannot_jointly_exceed_the_daily_ledger_cap():
    gw, log, outs, client, reserved = _concurrent_cap_run(ledger=lambda r: DailyLedger(int(2.5 * r), 1000))
    assert sorted(o.ok for o in outs) == [False, False, True, True] and len(client.calls) == 2
    assert all(o.failure == "ledger_cap" for o in outs if not o.ok)
    assert gw._held == 0 and gw.ledger.tokens == 200                       # actual spend only, no leftover holds


def test_a_hold_is_released_when_an_in_flight_call_is_cancelled():
    async def go():
        log = EventLog(None)
        gw = Gateway(FakeClient([(5.0, ok_result())]), cfg(pilot_token_cap=100000), log)
        task = asyncio.ensure_future(gw.call(ctx_for(log, deadline_s=30.0), MSGS, max_tokens=50, purpose="draft"))
        await asyncio.sleep(0.05)
        held_in_flight = gw._held
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return held_in_flight, gw._held
    in_flight, after = asyncio.run(go())
    assert in_flight > 0 and after == 0


def test_daily_ledger_cap_is_checked_against_the_full_reservation_too():
    ledger = DailyLedger(token_cap=100, request_cap=100)
    ledger.tokens = 50                          # 50 spent, but the next call would reserve about 90 more
    client = FakeClient([ok_result()])
    gw, log, (out,), _ = run_call(client, cfg(), ledger=ledger)
    assert not out.ok and out.failure == "ledger_cap" and len(client.calls) == 0
    assert events_named(log, "cap_check")[0]["cap"] == "daily_ledger"
    assert status_for("pilot_cap", None) == "rate_limited" and status_for("ledger_cap", None) == "rate_limited"


def test_failed_call_without_usage_is_not_reported_as_provider_tokens():
    client = FakeClient([err_result("timeout"), ok_result(tin=60, tout=40)])
    _, log, (out,), (ctx,) = run_call(client, cfg())
    assert out.ok
    assert (ctx.tokens_in, ctx.tokens_out) == (60, 40)      # only the successful call's provider usage
    assert ctx.tokens_unaccounted_est > 0                   # the timed-out call is tracked separately, as an estimate


def test_window_wait_semantics():
    w = _Window(limit=100.0, hard=150.0, span=60.0)
    assert w.wait_for(120.0, 0.0) == 0.0            # a lone call fits even above the soft limit
    assert w.wait_for(200.0, 0.0) == math.inf       # but not above the hard limit
    w.add(0.0, 90.0)
    assert w.wait_for(5.0, 10.0) == 0.0
    assert abs(w.wait_for(20.0, 10.0) - 50.0) < 1e-9   # must wait for the entry at t=0 to leave the window
    assert w.used(61.0) == 0.0


def test_daily_ledger_persists_resets_by_date_and_caps(tmp_path):
    p = tmp_path / "ledger.json"
    a = DailyLedger(token_cap=250, request_cap=10, path=p, today="2026-11-13")
    a.record(100)
    a.record(100)
    assert not a.exhausted
    b = DailyLedger(token_cap=250, request_cap=10, path=p, today="2026-11-13")
    assert (b.tokens, b.requests) == (200, 2)
    b.record(60)
    assert b.exhausted
    c = DailyLedger(token_cap=250, request_cap=10, path=p, today="2026-11-14")
    assert (c.tokens, c.requests) == (0, 0) and not c.exhausted
    d = DailyLedger(token_cap=250, request_cap=10, today="x")
    d.mark_provider_limit()
    assert d.exhausted


def test_status_mapping():
    assert status_for("deadline", None) == "timeout"
    assert status_for("deadline_no_retry", "rate_limit_minute") == "rate_limited"
    assert status_for("deadline_no_retry", "server_error") == "error"
    assert status_for("limiter_timeout", None) == "rate_limited"
    assert status_for("daily_exhausted", "rate_limit_day") == "rate_limited"
    assert status_for("retries_exhausted", "rate_limit_minute") == "rate_limited"
    assert status_for("retries_exhausted", "server_error") == "error"
    assert status_for("retries_exhausted", "timeout") == "timeout"
    assert status_for("client_error", "client_error") == "error"
    assert CallOutcome(ok=True).status == "ok"
