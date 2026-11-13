from __future__ import annotations

import asyncio

from helpers import FakeClient, ctx_for, err_result, events_named, ok_result

from relay.admission import Admission
from relay.config import PaperSpec, RetrievalSetting
from relay.gateway import DailyLedger, Gateway, GatewayConfig
from relay.pipeline import BUSY_TEXT, Pipeline, PipelineConfig, reformulate
from relay.retrieve import Retriever
from relay.schema import Chunk
from relay.tracing import EventLog

GOOD = "The encoder is a stack of N = 6 identical layers [S1]."
Q = "How many identical layers are in the encoder stack?"
UNANSWERABLE = "What is the GPU price of training GPT-4?"


def chunk(cid, paper, text, page=3):
    return Chunk(chunk_id=cid, paper_key=paper, setting_id="S1", page_start=page, page_end=page,
                 section="3.1", text=text, approx_tokens=len(text) // 4, printed_start=str(page - 1),
                 printed_end=str(page - 1))


CHUNKS = [
    chunk("a1", "attention", "The encoder is composed of a stack of N = 6 identical layers. Each layer has two sub-layers."),
    chunk("m1", "moshi", "Mimi is a neural audio codec that uses residual vector quantization to produce audio tokens.", 9),
    chunk("d1", "dynamic-chunking", "H-Net learns segment boundaries with a routing module and a smoothing module.", 5),
]
SETTING = RetrievalSetting(id="S1", chunk_tokens=100, overlap_tokens=10, k=2, fetch_k=4, rerank="none", section_aware=False)
PAPERS = [
    PaperSpec("attention", "1706.03762", 7, "Attention Is All You Need"),
    PaperSpec("moshi", "2410.00037", 1, "Moshi: a speech-text foundation model for real-time dialogue"),
]


def gcfg(**kw) -> GatewayConfig:
    base = dict(rpm=1000, tpm=100000, headroom=1.0, backoff_base_s=0.01, backoff_cap_s=0.05, pause_jitter_s=0.0,
                reserve_s=0.05, initial_call_estimate_s=0.05, call_timeout_s=1.0, first_token_timeout_s=None,
                breaker_threshold=99)
    base.update(kw)
    return GatewayConfig(**base)


def pcfg(**kw) -> PipelineConfig:
    base = dict(setting="S1", deadline_s=5.0, tool_timeout_s=1.0, reserve_s=0.05, ladder_enabled=False)
    base.update(kw)
    return PipelineConfig(**base)


def run(script, requests=((Q, 5.0),), *, p=None, g=None, n=2, cap=5, ledger=None, concurrent=False):
    """Run one or more requests through a pipeline wired to a scripted client."""
    async def go():
        log = EventLog(None)
        client = FakeClient(script)
        gw = Gateway(client, g or gcfg(), log, ledger=ledger)
        adm = Admission(n, cap, newest_first_after_s=100.0, min_service_s=0.05, initial_service_s=0.1, estimate_check=False)
        retr = Retriever(SETTING, CHUNKS, abstain_min_coverage=0.5, mmr_lambda=0.7)
        pipe = Pipeline({"S1": retr}, PAPERS, gw, adm, p or pcfg(), log)
        ctxs = [ctx_for(log, deadline_s=d, rid=f"r{i}", question=q) for i, (q, d) in enumerate(requests)]
        if concurrent:
            outs = await asyncio.gather(*[pipe.handle(c, c.question) for c in ctxs])
        else:
            outs = [await pipe.handle(c, c.question) for c in ctxs]
        return log, client, gw, adm, pipe, ctxs, outs
    return asyncio.run(go())


def names(log, rid=None):
    return [e["event"] for e in log.events if rid is None or e["request_id"] == rid]


def test_happy_path_buffer_verify_release():
    log, client, gw, adm, _, (ctx,), (out,) = run([ok_result(text=GOOD)])
    assert out.status == "ok" and out.answer_mode == "llm" and out.v0_passed is True
    assert "[attention, pdf p.3 (printed 2), §3.1]" in out.answer and "[S1]" not in out.answer
    assert 0 < out.first_token_ms <= out.total_ms
    assert (ctx.tokens_in, ctx.tokens_out, ctx.retries) == (60, 40, 0)
    order = names(log, "r0")
    for a, b in [("arrive", "slot_acquired"), ("slot_acquired", "tool_start"), ("tool_end", "decide"),
                 ("decide", "send"), ("call_end", "verify_result"), ("verify_result", "first_token"),
                 ("first_token", "slot_released"), ("slot_released", "done")]:
        assert order.index(a) < order.index(b), (a, b, order)
    assert order.count("done") == 1
    done = events_named(log, "done")[0]
    assert done["retrieval_setting"] == "S1" and done["status"] == "ok"
    assert adm.inflight == 0


def test_user_visible_first_token_is_release_time_not_model_first_token():
    log, *_ , (out,) = run([(0.2, ok_result(text=GOOD))])
    mft = events_named(log, "model_first_token")[0]
    ft = events_named(log, "first_token")[0]
    assert ft["t_mono"] >= mft["t_mono"] and out.first_token_ms >= 190


def test_unanswerable_question_is_refused_without_a_model_call():
    log, client, *_, (out,) = run([], requests=((UNANSWERABLE, 5.0),))
    assert out.status == "ok" and out.answer_mode == "refusal" and out.abstained
    assert len(client.calls) == 0
    assert events_named(log, "decide")[0]["branch"] == "refuse_no_evidence"


def test_model_refusal_passes_v0_and_is_reported_as_refusal():
    *_, (out,) = run([ok_result(text="The papers do not say.")])
    assert out.status == "ok" and out.answer_mode == "refusal" and out.v0_passed


def test_v0_failure_triggers_one_discretionary_regeneration():
    bad = ok_result(text="The encoder has 12 identical layers [S1].")
    log, client, *_, (ctx,), (out,) = run([bad, ok_result(text=GOOD)])
    assert out.status == "ok" and out.answer_mode == "llm" and out.regenerated
    sends = events_named(log, "send")
    assert [(s["purpose"], s["discretionary"]) for s in sends] == [("draft", False), ("regenerate", True)]
    assert "ungrounded_number" in events_named(log, "verify_result")[0]["failures"]
    assert "ungrounded_number" in client.calls[1]["messages"][0]["content"]       # the strict prompt names the reason
    assert (ctx.retries, ctx.discretionary_calls) == (0, 1)


def test_answer_that_fails_v0_twice_is_never_released_extractive_fallback_is():
    bad = ok_result(text="The encoder has 12 identical layers [S1].")
    log, client, *_, (out,) = run([bad, bad])
    assert out.status == "ok" and out.answer_mode == "extractive"
    assert "12" not in out.answer and "N = 6 identical layers" in out.answer
    attempts = [(e["attempt"], e["passed"]) for e in events_named(log, "verify_result")]
    assert attempts == [(1, False), (2, False), (3, True)]


def test_events_record_the_checked_draft_exact_failure_reasons_and_released_answer():
    draft = "The encoder has 12 identical layers [S1]."
    bad = ok_result(text=draft)
    log, client, *_, (out,) = run([bad, bad])
    v = events_named(log, "verify_result")
    # attempt 1: the model's draft exactly as checked, with the exact reason and offending value
    assert v[0]["attempt"] == 1 and v[0]["text"] == draft and v[0]["passed"] is False
    assert v[0]["failures"] == ["ungrounded_number"] and v[0]["details"] == ["ungrounded_number: 12"]
    # attempt 2: the regenerated draft (same scripted text here)
    assert v[1]["attempt"] == 2 and v[1]["text"] == draft and v[1]["details"] == ["ungrounded_number: 12"]
    # attempt 3: the extractive fallback that was actually released, which passes with no details
    assert v[2]["attempt"] == 3 and v[2]["passed"] is True and v[2]["details"] == []
    assert "N = 6 identical layers" in v[2]["text"]
    # the user-visible event carries the final answer with citations rendered, identical to the outcome
    ft = events_named(log, "first_token")[0]
    assert ft["answer"] == out.answer and ft["mode"] == "extractive"
    assert "[S1]" not in ft["answer"] and "[attention, pdf p.3" in ft["answer"]


def test_released_answer_is_logged_for_llm_answers_and_free_refusals_too():
    log, *_, (out,) = run([ok_result(text=GOOD)])
    assert events_named(log, "first_token")[0]["answer"] == out.answer and "N = 6 identical layers" in out.answer
    assert events_named(log, "verify_result")[0]["text"] == GOOD and events_named(log, "verify_result")[0]["details"] == []
    log, *_, (out,) = run([], requests=((UNANSWERABLE, 5.0),))
    assert events_named(log, "first_token")[0]["answer"] == out.answer and not events_named(log, "verify_result")


def test_with_regeneration_on_a_twice_failed_draft_ends_extractive_and_reports_the_regeneration():
    # Default test config: regenerate=True. Two model calls (draft, regenerate), then the extractive fallback.
    bad = ok_result(text="The encoder has 12 identical layers [S1].")
    log, client, *_, (out,) = run([bad, bad])
    assert (out.status, out.answer_mode, out.v0_passed, out.regenerated) == ("ok", "extractive", True, True)
    assert len(client.calls) == 2
    assert out.v0_failures == []                        # the outcome reflects the released (extractive) answer, as before


def test_pilot_configuration_regeneration_off_goes_straight_to_extractive_after_one_model_call():
    # Mirrors the quality_measure pilot (regenerate: false): attempts are 1 (the draft) and 3 (the extractive text).
    draft = "The encoder has 12 identical layers [S1]."
    log, client, *_, (out,) = run([ok_result(text=draft)], p=pcfg(regenerate=False))
    assert len(client.calls) == 1 and not out.regenerated                   # one model call, no regeneration
    assert (out.status, out.answer_mode, out.v0_passed) == ("ok", "extractive", True)
    v = events_named(log, "verify_result")
    assert [(e["attempt"], e["passed"]) for e in v] == [(1, False), (3, True)]
    assert v[0]["text"] == draft and v[0]["details"] == ["ungrounded_number: 12"]
    assert [e["purpose"] for e in events_named(log, "send")] == ["draft"]    # no "regenerate" send
    assert events_named(log, "degrade")[0]["reason"] == "v0_failed"
    ft = events_named(log, "first_token")[0]
    assert ft["mode"] == "extractive" and ft["answer"] == out.answer


def test_long_checked_text_is_truncated_in_the_log_only():
    long_text = ("The encoder has identical layers [S1]. " * 100).strip()
    log, *_ = run([ok_result(text=long_text), ok_result(text=long_text)])
    assert len(events_named(log, "verify_result")[0]["text"]) <= 2000


def test_unverifiable_everything_ends_in_an_honest_refusal():
    # The retrieved passage is too short to quote (under 6 words), so there is no extractive fallback either.
    bad = ok_result(text="Zebras migrate across savannas every single season [S1].")
    chunks_off = [chunk("a1", "attention", "Irrelevant words here.")]
    async def go():
        log = EventLog(None)
        client = FakeClient([bad, bad])
        gw = Gateway(client, gcfg(), log)
        adm = Admission(1, 1, estimate_check=False)
        retr = Retriever(SETTING, chunks_off, abstain_min_coverage=0.0, mmr_lambda=0.7)
        pipe = Pipeline({"S1": retr}, PAPERS, gw, adm, pcfg(), log)
        return await pipe.handle(ctx_for(log, question="zebras migrate irrelevant"), "zebras migrate irrelevant")
    out = asyncio.run(go())
    assert out.status == "ok" and out.answer_mode == "refusal" and "not giving one" in out.answer
    assert out.fail_reason and out.fail_reason.startswith("unverified")


def test_provider_errors_after_retries_are_reported_as_error_with_no_answer():
    script = [err_result("server_error", 503)] * 3
    log, client, *_, (out,) = run(script)
    assert out.status == "error" and out.answer == "" and out.first_token_ms is None
    assert out.fail_stage == "draft" and out.fail_reason == "retries_exhausted"


def test_no_affordable_quota_degrades_to_extractive_when_the_ladder_is_off():
    # rpm=1: the first request uses the minute's only request; the second cannot wait 60 s.
    log, client, *_, outs = run([ok_result(text=GOOD)], requests=((Q, 5.0), (Q, 2.0)), g=gcfg(rpm=1))
    assert outs[0].answer_mode == "llm"
    assert outs[1].status == "ok" and outs[1].answer_mode == "extractive"
    assert len(client.calls) == 1
    assert any(e["event"] == "degrade" and e.get("reason") == "limiter_timeout" for e in log.events)


def test_ladder_level_3_goes_extractive_without_a_model_call_but_still_verifies():
    p = pcfg(ladder_enabled=True)
    log, client, *_, outs = run([ok_result(text=GOOD)], requests=((Q, 5.0), (Q, 5.0)), g=gcfg(rpm=1), p=p)
    assert outs[1].ladder_level == 3 and outs[1].answer_mode == "extractive"
    assert len(client.calls) == 1
    assert [e["passed"] for e in events_named(log, "verify_result") if e["request_id"] == "r1"] == [True]


def test_ladder_level_4_when_daily_budget_is_exhausted_gives_honest_busy():
    ledger = DailyLedger(token_cap=100, request_cap=10)
    ledger.record(100)
    p = pcfg(ladder_enabled=True)
    log, client, *_, (out,) = run([], p=p, ledger=ledger)
    assert out.status == "rate_limited" and out.answer_mode == "busy" and out.answer == BUSY_TEXT
    assert out.ladder_level == 4 and len(client.calls) == 0 and out.fail_stage == "draft"


def test_ladder_level_4_still_refuses_unanswerable_questions_for_free():
    ledger = DailyLedger(token_cap=100, request_cap=10)
    ledger.record(100)
    log, client, *_, (out,) = run([], requests=((UNANSWERABLE, 5.0),), p=pcfg(ladder_enabled=True), ledger=ledger)
    assert out.ladder_level == 4 and out.status == "ok" and out.answer_mode == "refusal" and len(client.calls) == 0


def test_daily_429_mid_request_is_busy_not_retried():
    log, client, *_, (out,) = run([err_result("rate_limit_day", 429, retry_after=7200)])
    assert out.status == "rate_limited" and out.answer_mode == "busy" and len(client.calls) == 1
    assert out.fail_reason == "daily_exhausted"


def test_shed_at_the_door_is_a_timeout_with_the_finer_reason_recorded():
    slow = (0.3, ok_result(text=GOOD))
    log, client, gw, adm, _, ctxs, outs = run([slow], requests=((Q, 5.0), (Q, 5.0)), n=1, cap=0, concurrent=True)
    shed = [o for o in outs if o.shed_reason]
    assert len(shed) == 1 and shed[0].status == "timeout" and shed[0].shed_reason == "queue_full"
    assert shed[0].fail_stage == "admit" and shed[0].fail_reason == "shed:queue_full"
    assert adm.inflight == 0


def test_deadline_expiry_cancels_the_request_and_frees_the_slot():
    log, client, gw, adm, _, (ctx,), (out,) = run([(2.0, ok_result(text=GOOD))], requests=((Q, 0.5),))
    assert out.status == "timeout" and out.fail_stage == "draft" and out.answer == ""
    assert out.total_ms < 900 and adm.inflight == 0
    assert names(log).count("done") == 1


def test_tool_failure_is_retried_once_then_reported_as_error():
    log, client, *_, (out,) = run([], p=pcfg(tool_fail_p=1.0))
    assert out.status == "error" and out.fail_stage == "retrieve" and len(client.calls) == 0
    assert [e["ok"] for e in events_named(log, "tool_end")] == [False, False]


def test_tool_timeout_is_reported_as_timeout():
    log, client, *_, (out,) = run([], p=pcfg(tool_slow_p=1.0, tool_slow_s=1.0, tool_timeout_s=0.05))
    assert out.status == "timeout" and out.fail_stage == "retrieve" and len(client.calls) == 0


def test_cache_serves_a_verified_answer_without_a_second_model_call():
    log, client, *_, outs = run([ok_result(text=GOOD)], requests=((Q, 5.0), (Q, 5.0)), p=pcfg(cache_enabled=True))
    assert outs[0].answer_mode == "llm" and outs[1].answer_mode == "cache"
    assert len(client.calls) == 1 and outs[1].answer == outs[0].answer and "[S1]" not in outs[1].answer


def test_paper_named_in_question_is_detected():
    log, *_ = run([ok_result(text=GOOD)],
                  requests=(("In Attention Is All You Need, how many identical layers are in the encoder?", 5.0),))
    assert events_named(log, "understand")[0]["papers"] == ["attention"]


def test_reformulate_drops_common_words_only_when_something_changes():
    retr = Retriever(SETTING, CHUNKS, abstain_min_coverage=0.5, mmr_lambda=0.7)
    assert reformulate("zebra", retr) is None                              # nothing to keep
    assert reformulate("encoder stack zebra", retr, min_idf=0.5) == "encoder stack"   # unknown word dropped
    assert reformulate("encoder stack", retr, min_idf=0.5) is None           # nothing would change


def test_concurrent_requests_never_exceed_the_in_flight_cap():
    script = [(0.05, ok_result(text=GOOD)) for _ in range(6)]
    log, client, gw, adm, *_ = run(script, requests=tuple((Q, 5.0) for _ in range(6)), n=2, cap=10, concurrent=True)
    assert adm.peak_inflight <= 2
    acquired = events_named(log, "slot_acquired")
    assert len(acquired) == 6
