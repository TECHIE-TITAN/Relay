from __future__ import annotations

from relay.artifacts import RESULT_COLUMNS
from relay.metrics import (
    CSV_FIELDS, check_invariants, csv_rows, dist_summary, percentile, request_row, request_rows, summarize,
)

MANIFEST = {"scenario": "t", "source": "standin", "wall_start_utc": "2026-11-13T10:00:00+00:00",
            "prices": {"in_per_mtok": 0.075, "out_per_mtok": 0.30}, "load_duration_s": 60.0,
            "provenance": "SIMULATED"}


def ev(t, event, rid="r1", **kw):
    d = {"t_mono": t, "request_id": rid, "stage": "x", "event": event}
    d.update(kw)
    return d


def full_request(rid="r1", t0=1.0):
    return [
        ev(t0, "arrive", rid),
        ev(t0 + 0.2, "slot_acquired", rid, wait_ms=200, remaining_ms=19000),
        ev(t0 + 0.21, "tool_end", rid, duration_ms=5),
        ev(t0 + 0.22, "limiter_wait_start", rid), ev(t0 + 0.32, "limiter_wait_end", rid, waited_ms=100),
        ev(t0 + 0.32, "send", rid, remaining_ms=18000, discretionary=False),
        ev(t0 + 0.37, "call_end", rid, ok=False, duration_ms=50),
        ev(t0 + 0.37, "retry_sleep_start", rid, sleep_ms=100), ev(t0 + 0.47, "retry_sleep_end", rid),
        ev(t0 + 0.47, "send", rid, remaining_ms=17900, discretionary=True),
        ev(t0 + 0.87, "call_end", rid, ok=True, duration_ms=400),
        ev(t0 + 0.88, "model_first_token", rid, ttft_ms=120.0),
        ev(t0 + 0.88, "verify_result", rid, ms=0.5),
        ev(t0 + 1.0, "done", rid, status="ok", total_ms=1000.0, first_token_ms=900.0, retries=1, tokens_in=60,
           tokens_out=40, retrieval_setting="S2", answer_mode="llm", attempts=2, discretionary_calls=1,
           question_id="q01"),
    ]


def test_percentile_matches_hand_computation():
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert abs(percentile(list(range(1, 101)), 95) - 95.05) < 1e-9
    assert percentile([7], 99) == 7.0 and percentile([], 50) is None
    assert percentile([5, 1, 3], 0) == 1.0 and percentile([5, 1, 3], 100) == 5.0


def test_dist_summary_flags_small_samples_and_never_returns_zero_for_nothing():
    empty = dist_summary([])
    assert empty["n"] == 0 and empty["p50"] is None and empty["note"] == "no samples"
    few = dist_summary([1.0, 2.0, 3.0])
    assert few["n"] == 3 and few["p50"] == 2.0 and "n < 20" in few["note"] and few["p99"] is None
    assert "too few" in few["p99_note"]
    mid = dist_summary([float(i) for i in range(1, 51)])
    assert mid["note"] is None and mid["p95"] is not None and mid["p99"] is None
    big = dist_summary([float(i) for i in range(1, 201)])
    assert big["p99"] is not None and "p99_note" not in big


def test_waiting_working_retrying_split_with_explicit_unaccounted_remainder():
    row = request_row(full_request(), MANIFEST)
    assert row["waiting_ms"] == 300.0                 # 200 queue + 100 limiter
    assert row["working_ms"] == 405.5                 # 400 call + 5 tool + 0.5 verify
    assert row["retrying_ms"] == 150.0                # 50 failed call + 100 back-off
    assert row["unaccounted_ms"] == 144.5             # not forced to sum
    assert row["start_time"] == "2026-11-13T10:00:01.000+00:00"
    assert row["model_first_token_ms"] == 120.0 and row["first_token_ms"] == 900.0
    assert row["answer_grade"] is None and row["status"] == "ok" and row["retries"] == 1


def test_shed_request_was_waiting_throughout_and_unfinished_requests_are_skipped():
    shed = [ev(0.0, "arrive", "s"), ev(0.0, "shed", "s", reason="queue_full"),
            ev(0.005, "done", "s", status="timeout", total_ms=5.0, shed_reason="queue_full", fail_stage="admit")]
    row = request_row(shed, MANIFEST)
    assert row["waiting_ms"] == 5.0 and row["working_ms"] == 0.0 and row["first_token_ms"] is None
    assert request_row([ev(0.0, "arrive", "u"), ev(0.1, "send", "u")], MANIFEST) is None


def test_call_cut_short_by_the_deadline_counts_as_working_until_done():
    evs = [ev(0.0, "arrive", "c"), ev(0.1, "slot_acquired", "c", wait_ms=100), ev(0.2, "send", "c"),
           ev(1.2, "done", "c", status="timeout", total_ms=1200.0, fail_stage="draft")]
    row = request_row(evs, MANIFEST)
    assert row["working_ms"] == 1000.0 and row["waiting_ms"] == 100.0


def test_csv_columns_are_exactly_the_briefs_and_match_artifacts():
    assert CSV_FIELDS == RESULT_COLUMNS
    rows = request_rows(full_request("a", 1.0) + full_request("b", 0.5), MANIFEST)
    assert [r["request_id"] for r in rows] == ["b", "a"]               # ordered by arrival
    assert list(csv_rows(rows)[0]) == list(RESULT_COLUMNS)


def test_summary_counts_costs_and_missing_data():
    s = full_request("a")
    shed = [ev(2.0, "arrive", "s"), ev(2.0, "shed", "s"),
            ev(2.005, "done", "s", status="timeout", total_ms=5.0, shed_reason="queue_full", fail_stage="admit")]
    rl = [ev(3.0, "arrive", "x"), ev(3.5, "done", "x", status="rate_limited", total_ms=500.0, retries=0,
                                     tokens_in=0, tokens_out=0, retrieval_setting="S2", fail_stage="draft")]
    open_req = [ev(4.0, "arrive", "o")]
    events = s + shed + rl + open_req + [ev(5.0, "gateway_state", None, queue_len=7, inflight=3)]
    rows = request_rows(events, MANIFEST)
    sm = summarize(rows, events, MANIFEST)
    assert sm["n_requests"] == 3 and sm["n_incomplete"] == 1
    assert sm["status_counts"] == {"ok": 1, "rate_limited": 1, "timeout": 1, "error": 0}
    assert sm["shed_reasons"] == {"queue_full": 1} and sm["peak_queue_len"] == 7
    assert abs(sm["notional_cost_usd"] - (60 * 0.075 + 40 * 0.30) / 1e6) < 1e-12
    assert sm["first_token_ms"]["n"] == 1 and abs(sm["goodput_per_min"] - 1.0) < 1e-9
    assert sm["retries_total"] == 1 and sm["degraded_ok"] == 0 and sm["tokens_per_answer"] == 100.0
    empty = summarize([], [], {"scenario": "t"})
    assert empty["ok_rate"] is None and empty["peak_queue_len"] is None and empty["notional_cost_usd"] is None
    assert empty["total_ms_ok"]["p50"] is None


def inv(ids, results):
    return {r["id"]: r for r in results}[ids]


P = {"max_attempts": 5, "n_inflight": 2, "reserve_s": 0.3, "discretionary_share": 0.15, "global_pause": True}


def test_invariants_pass_on_a_clean_trace():
    res = check_invariants(full_request(), {**P})
    assert all(r["passed"] for r in res if r["applicable"])


def test_i1_and_i2_detect_late_sends_and_too_many_attempts():
    late = [ev(1.0, "send", "r", remaining_ms=100, discretionary=False)]
    assert inv("I1", check_invariants(late, P))["passed"] is False
    many = [ev(float(i), "send", "r", remaining_ms=9000, discretionary=False) for i in range(6)]
    r = inv("I2", check_invariants(many, P))
    assert r["passed"] is False and r["violations"][0]["attempts"] == 6


def test_i3_detects_a_retry_heavy_window_but_ignores_small_windows():
    heavy = [ev(float(i), "send", "r%d" % i, remaining_ms=9000, discretionary=(i % 2 == 0)) for i in range(30)]
    assert inv("I3", check_invariants(heavy, P))["passed"] is False
    small = heavy[:10]
    assert inv("I3", check_invariants(small, P))["passed"] is True
    assert inv("I3", check_invariants(heavy, {**P, "discretionary_share": None}))["applicable"] is False


def test_i4_detects_more_pipelines_than_the_cap_and_handles_ties():
    ok = [ev(0.0, "slot_acquired", "a"), ev(0.0, "slot_acquired", "b"), ev(1.0, "slot_released", "a"),
          ev(1.0, "slot_acquired", "c"), ev(2.0, "slot_released", "b"), ev(2.0, "slot_released", "c")]
    assert inv("I4", check_invariants(ok, P))["passed"] is True
    bad = ok[:2] + [ev(0.5, "slot_acquired", "c")]
    assert inv("I4", check_invariants(bad, P))["passed"] is False


def test_i5_detects_admission_after_the_deadline():
    bad = [ev(1.0, "slot_acquired", "a", remaining_ms=-5.0)]
    assert inv("I5", check_invariants(bad, P))["passed"] is False


def test_i6_detects_dispatch_during_a_global_pause_and_respects_the_switch():
    events = [ev(1.0, "pause_start", None, until_mono=3.0), ev(2.0, "send", "a", remaining_ms=9000),
              ev(3.5, "send", "b", remaining_ms=9000)]
    r = inv("I6", check_invariants(events, P))
    assert r["passed"] is False and r["n_violations"] == 1
    assert inv("I6", check_invariants(events, {**P, "global_pause": False}))["applicable"] is False
