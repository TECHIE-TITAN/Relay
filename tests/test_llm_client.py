from __future__ import annotations

import asyncio
import json

import httpx

from relay.llm_client import RETRYABLE, LLMClient, classify_429
from relay.standin.profile import StandinProfile
from relay.standin.server import create_app

MSGS = [{"role": "user", "content": "Sources:\n[S1] attention | pdf p.3\nSix identical layers.\n\nQuestion: how many?"}]


def sse(obj) -> bytes:
    return ("data: " + json.dumps(obj) + "\n\n").encode()


def delta(text=None, reasoning=None, finish=None):
    d = {}
    if text is not None:
        d["content"] = text
    if reasoning is not None:
        d["reasoning"] = reasoning
    return sse({"choices": [{"index": 0, "delta": d, "finish_reason": finish}]})


def mock_client(handler):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return LLMClient("http://mock/v1", "m", client=http, stream_options=True), http


def run_mock(handler, **kw):
    async def go():
        llm, http = mock_client(handler)
        try:
            return await llm.chat(MSGS, max_tokens=50, **kw)
        finally:
            await http.aclose()
    return asyncio.run(go())


def stream_response(gen, status=200, headers=None):
    return httpx.Response(status, headers=headers or {"content-type": "text/event-stream"}, content=gen)


def fast(**kw) -> StandinProfile:
    base = dict(ttft_median_ms=1.0, ttft_sigma=0.0, tokens_per_s=1e6, p_5xx=0.0, p_hang=0.0,
                p_slow=0.0, p_midstream=0.0, p_bad_citation=0.0)
    base.update(kw)
    return StandinProfile(**base)


def run_standin(profile, n=1, **client_kw):
    app = create_app(profile)

    async def go():
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://standin")
        llm = LLMClient("http://standin/v1", "m", client=http, **client_kw)
        try:
            return [await llm.chat(MSGS, max_tokens=50, timeout_s=5) for _ in range(n)]
        finally:
            await http.aclose()
    return asyncio.run(go())


def test_success_against_standin_reads_provider_usage_and_headers():
    (r,) = run_standin(fast())
    assert r.ok and "[S1]" in r.text
    assert r.usage_source == "provider" and r.tokens_in > 0 and r.tokens_out > 0
    assert r.ttft_ms is not None and r.ttft_ms <= r.total_ms
    assert r.headers["x-ratelimit-limit-tokens"] == "8000"


def test_usage_in_x_groq_when_stream_options_not_sent():
    (r,) = run_standin(fast(), stream_options=False)
    assert r.ok and r.usage_source == "provider" and r.tokens_out > 0


def test_minute_429_is_classified_retryable_with_retry_after():
    _, r = run_standin(fast(rpm=1), n=2)
    assert not r.ok and r.error_kind == "rate_limit_minute" and r.status_code == 429
    assert r.retry_after_s is not None and r.retry_after_s >= 1
    assert r.error_kind in RETRYABLE and r.classified_by == "message"


def test_day_429_is_classified_by_message_and_not_retryable():
    _, r = run_standin(fast(rpd=1), n=2)
    assert r.error_kind == "rate_limit_day" and r.classified_by == "message"
    assert r.error_kind not in RETRYABLE


def test_classify_429_rules():
    assert classify_429({"retry-after": "600"}, "", 120) == ("rate_limit_day", "retry_after")
    assert classify_429({"retry-after": "5"}, "", 120) == ("rate_limit_minute", "default")
    assert classify_429({}, "limit on tokens per day (TPD)", 120) == ("rate_limit_day", "message")
    assert classify_429({"retry-after": "5"}, "tokens per minute (TPM)", 120) == ("rate_limit_minute", "message")


def test_server_error_and_client_error_kinds():
    (r5,) = run_standin(fast(p_5xx=1.0))
    assert r5.error_kind == "server_error" and r5.status_code == 503 and r5.error_kind in RETRYABLE
    (r4,) = run_standin(fast(tpm=40))      # prompt (~20 tokens) + max output (50) exceeds 40
    assert r4.error_kind == "client_error" and r4.status_code == 413 and r4.error_kind not in RETRYABLE


def test_midstream_drop_is_reported_as_midstream():
    (r,) = run_standin(fast(p_midstream=1.0))
    assert not r.ok and r.error_kind == "midstream"


def test_reasoning_deltas_do_not_count_as_first_token():
    async def gen():
        yield delta(reasoning="thinking...")
        await asyncio.sleep(0.15)
        yield delta(text="Answer [S1].")
        yield delta(finish="stop")
        yield b"data: [DONE]\n\n"

    r = run_mock(lambda req: stream_response(gen()), timeout_s=5)
    assert r.ok and r.text == "Answer [S1]."
    assert r.first_byte_ms < 100 and r.ttft_ms >= 140


def test_missing_usage_is_marked_estimated_not_provider():
    async def gen():
        yield delta(text="Answer [S1].")
        yield b"data: [DONE]\n\n"

    r = run_mock(lambda req: stream_response(gen()), timeout_s=5)
    assert r.ok and r.usage_source == "estimated" and r.tokens_out > 0 and r.tokens_in == 0


def test_first_token_timeout():
    async def gen():
        await asyncio.sleep(1.0)
        yield delta(text="late")
        yield b"data: [DONE]\n\n"

    r = run_mock(lambda req: stream_response(gen()), timeout_s=5, first_token_timeout_s=0.1)
    assert not r.ok and r.error_kind == "first_token_timeout" and r.total_ms < 600


def test_whole_call_timeout_keeps_partial_text():
    async def gen():
        yield delta(text="partial ")
        await asyncio.sleep(1.0)
        yield b"data: [DONE]\n\n"

    r = run_mock(lambda req: stream_response(gen()), timeout_s=0.15)
    assert not r.ok and r.error_kind == "timeout" and r.text == "partial " and r.ttft_ms is not None


def test_connect_error_kind():
    def handler(req):
        raise httpx.ConnectError("refused")

    r = run_mock(handler, timeout_s=5)
    assert not r.ok and r.error_kind == "connect"


def test_transport_failure_after_headers_is_midstream():
    async def gen():
        yield delta(text="part")
        raise httpx.ReadError("connection reset")

    r = run_mock(lambda req: stream_response(gen()), timeout_s=5)
    assert not r.ok and r.error_kind == "midstream" and r.text == "part"


def test_empty_reply_is_not_retryable():
    async def gen():
        yield delta(reasoning="thinking only")
        yield delta(finish="length")
        yield b"data: [DONE]\n\n"

    r = run_mock(lambda req: stream_response(gen()), timeout_s=5)
    assert not r.ok and r.error_kind == "empty_reply" and r.error_kind not in RETRYABLE


def test_request_body_and_auth_header():
    seen = {}

    async def gen():
        yield delta(text="ok [S1].")
        yield delta(finish="stop")
        yield b"data: [DONE]\n\n"

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers.get("authorization")
        return stream_response(gen())

    async def go():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        llm = LLMClient("http://mock/v1", "m", api_key="k", client=http, extra_params={"reasoning_effort": "low"})
        r = await llm.chat(MSGS, max_tokens=77, timeout_s=5)
        await http.aclose()
        return r

    assert asyncio.run(go()).ok
    assert seen["body"]["stream"] is True and seen["body"]["max_completion_tokens"] == 77
    assert seen["body"]["reasoning_effort"] == "low"
    assert seen["auth"] == "Bearer k"
