from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from relay.standin.profile import StandinProfile
from relay.standin.server import create_app

PROMPT = (
    "Sources:\n[S1] attention | pdf p.3 | section 3.1\n"
    "The encoder is a stack of six identical layers. Each layer has two sub-layers.\n\n"
    "[S2] moshi | pdf p.4\nOther text.\n\nQuestion: How many layers?"
)


def fast(**kw) -> StandinProfile:
    base = dict(ttft_median_ms=1.0, ttft_sigma=0.0, tokens_per_s=1e6, p_5xx=0.0, p_hang=0.0,
                p_slow=0.0, p_midstream=0.0, p_bad_citation=0.0)
    base.update(kw)
    return StandinProfile(**base)


def run(app, fn):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://standin") as c:
            return await fn(c)
    return asyncio.run(go())


def body(**kw):
    d = {"model": "m", "messages": [{"role": "user", "content": PROMPT}], "max_completion_tokens": 100}
    d.update(kw)
    return d


def sse_events(text: str):
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            out.append(line[6:])
    return out


def test_non_stream_reply_quotes_source_and_reports_usage_and_headers():
    app = create_app(fast())
    r = run(app, lambda c: c.post("/v1/chat/completions", json=body()))
    assert r.status_code == 200
    msg = r.json()["choices"][0]["message"]["content"]
    assert "stack of six identical layers" in msg and msg.endswith("[S1].")
    u = r.json()["usage"]
    assert u["total_tokens"] == u["prompt_tokens"] + u["completion_tokens"] > 0
    assert r.headers["x-ratelimit-limit-tokens"] == "8000"      # TPM
    assert r.headers["x-ratelimit-limit-requests"] == "1000"    # RPD
    assert "retry-after" not in r.headers


def test_stream_with_usage_chunk_and_done_marker():
    app = create_app(fast())
    r = run(app, lambda c: c.post("/v1/chat/completions", json=body(stream=True, stream_options={"include_usage": True})))
    ev = sse_events(r.text)
    assert ev[-1] == "[DONE]"
    objs = [json.loads(e) for e in ev[:-1]]
    text = "".join(o["choices"][0]["delta"].get("content", "") for o in objs if o["choices"])
    assert text.endswith("[S1].") and "six identical layers" in text
    assert objs[-1]["choices"] == [] and objs[-1]["usage"]["completion_tokens"] > 0
    assert any(o["choices"] and o["choices"][0]["finish_reason"] == "stop" for o in objs)


def test_stream_without_include_usage_puts_usage_in_x_groq():
    app = create_app(fast())
    r = run(app, lambda c: c.post("/v1/chat/completions", json=body(stream=True)))
    objs = [json.loads(e) for e in sse_events(r.text)[:-1]]
    assert "x_groq" in objs[-1] and objs[-1]["x_groq"]["usage"]["prompt_tokens"] > 0
    assert not any("usage" in o for o in objs)


def test_per_minute_request_limit_returns_429_with_retry_after():
    app = create_app(fast(rpm=2))

    async def go(c):
        return [await c.post("/v1/chat/completions", json=body()) for _ in range(3)]

    rs = run(app, go)
    assert [r.status_code for r in rs] == [200, 200, 429]
    assert "(RPM)" in rs[2].json()["error"]["message"]
    assert int(rs[2].headers["retry-after"]) >= 1
    assert rs[2].json()["error"]["type"] == "requests"


def test_per_minute_token_limit_counts_prompt_plus_max_output():
    # Two requests in flight together: each reserves ~50 prompt + 600 max output tokens,
    # so the second does not fit in 1000. (The reservation shrinks to actual usage on completion.)
    async def go(c):
        return await asyncio.gather(
            c.post("/v1/chat/completions", json=body(max_completion_tokens=600)),
            c.post("/v1/chat/completions", json=body(max_completion_tokens=600)),
        )
    a, b = run(create_app(fast(tpm=1000)), go)
    assert sorted([a.status_code, b.status_code]) == [200, 429]
    b = a if a.status_code == 429 else b
    assert "(TPM)" in b.json()["error"]["message"]


def test_daily_limit_has_long_retry_after_and_day_wording():
    app = create_app(fast(rpd=1, daily_retry_after_s=7200.0))

    async def go(c):
        return await c.post("/v1/chat/completions", json=body()), await c.post("/v1/chat/completions", json=body())

    ok, limited = run(app, go)
    assert ok.status_code == 200 and limited.status_code == 429
    assert "(RPD)" in limited.json()["error"]["message"]
    assert int(limited.headers["retry-after"]) == 7200


def test_request_larger_than_tpm_is_413_not_429():
    app = create_app(fast(tpm=100))
    r = run(app, lambda c: c.post("/v1/chat/completions", json=body(max_completion_tokens=500)))
    assert r.status_code == 413


def test_injected_5xx():
    app = create_app(fast(p_5xx=1.0))
    r = run(app, lambda c: c.post("/v1/chat/completions", json=body()))
    assert r.status_code == 503 and r.json()["error"]["type"] == "server_error"


def test_midstream_drop_ends_without_finish_or_done():
    app = create_app(fast(p_midstream=1.0))
    r = run(app, lambda c: c.post("/v1/chat/completions", json=body(stream=True)))
    ev = sse_events(r.text)
    assert "[DONE]" not in ev
    assert not any('"finish_reason":"stop"' in e for e in ev)


def test_bad_citation_probability_cites_a_source_not_provided():
    app = create_app(fast(p_bad_citation=1.0))
    r = run(app, lambda c: c.post("/v1/chat/completions", json=body()))
    assert "[S9]" in r.json()["choices"][0]["message"]["content"]


def test_no_sources_in_prompt_gives_the_refusal_sentence():
    app = create_app(fast())
    b = body(messages=[{"role": "user", "content": "Question: anything"}])
    r = run(app, lambda c: c.post("/v1/chat/completions", json=b))
    assert r.json()["choices"][0]["message"]["content"] == "The papers do not say."


def test_control_changes_profile_and_reset_clears_windows():
    app = create_app(fast(rpm=1))

    async def go(c):
        a = await c.post("/v1/chat/completions", json=body())
        b = await c.post("/v1/chat/completions", json=body())
        ctl = await c.post("/control", json={"set": {"rpm": 5}, "reset": True})
        d = await c.post("/v1/chat/completions", json=body())
        s = await c.get("/stats")
        return a, b, ctl, d, s

    a, b, ctl, d, s = run(app, go)
    assert (a.status_code, b.status_code, d.status_code) == (200, 429, 200)
    assert ctl.json()["profile"]["rpm"] == 5
    assert s.json()["rpd_used"] == 1 and s.json()["counts"]["ok"] == 1


def test_control_rejects_unknown_keys_and_invalid_body_is_400():
    app = create_app(fast())

    async def go(c):
        a = await c.post("/control", json={"set": {"nope": 1}})
        b = await c.post("/v1/chat/completions", content=b"not json")
        d = await c.post("/v1/chat/completions", json={"messages": []})
        return a, b, d

    a, b, d = run(app, go)
    assert a.status_code == 400 and b.status_code == 400 and d.status_code == 400


def test_profile_validation_and_scaling():
    with pytest.raises(ValueError):
        StandinProfile.from_dict({"bogus": 1})
    p = StandinProfile().scaled_limits(5)
    assert (p.rpm, p.tpm, p.rpd, p.tpd) == (150, 40000, 5000, 1000000)
    assert StandinProfile().rpm == 30       # original untouched
