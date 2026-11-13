from __future__ import annotations

import asyncio
import json

import pytest

from relay.cli import main
from relay.gateway import DailyLedger
from relay.llm_client import LLMClient
from relay.probe import ProbeEnv, run_probe
from relay.schema import RelayError


def env_for(url, tmp_path, ledger=None):
    client = LLMClient(f"{url}/v1", "m", stream_options=True)
    return client, ProbeEnv(client, ledger or DailyLedger(10 ** 9, 10 ** 6), tmp_path / "probe_real.jsonl", "m", "stand-in")


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_single_stage_records_a_raw_row_with_headers_and_provenance(standin_url, tmp_path):
    async def go():
        client, env = env_for(standin_url, tmp_path)
        try:
            return await run_probe(env, "single")
        finally:
            await client.aclose()
    lines = asyncio.run(go())
    assert any("headers returned" in line for line in lines)
    (r,) = rows(tmp_path / "probe_real.jsonl")
    assert r["stage"] == "single" and r["ok"] and r["usage_source"] == "provider"
    assert "x-ratelimit-limit-tokens" in r["headers"] and r["provenance"].startswith("SIMULATED")


def test_limit_stage_stops_at_the_first_429_and_records_it(standin_url, tmp_path):
    async def go():
        client, env = env_for(standin_url, tmp_path)
        try:
            return await run_probe(env, "limit", max_calls=60, recovery=False)
        finally:
            await client.aclose()
    lines = asyncio.run(go())
    assert any("FIRST 429" in line for line in lines)
    rs = rows(tmp_path / "probe_real.jsonl")
    limited = [r for r in rs if r["error_kind"] == "rate_limit_minute"]
    assert limited and limited[0]["retry_after_s"] >= 1 and len(rs) <= 42      # stand-in RPM is 30; stopped soon after


def test_probe_stops_when_the_daily_ledger_cap_is_reached(standin_url, tmp_path):
    async def go():
        client, env = env_for(standin_url, tmp_path, DailyLedger(token_cap=1, request_cap=100))
        try:
            await run_probe(env, "single")
            await run_probe(env, "single")
        finally:
            await client.aclose()
    with pytest.raises(RelayError, match="ledger"):
        asyncio.run(go())
    assert len(rows(tmp_path / "probe_real.jsonl")) == 1


def test_unknown_stage_is_rejected(standin_url, tmp_path):
    async def go():
        client, env = env_for(standin_url, tmp_path)
        try:
            await run_probe(env, "everything")
        finally:
            await client.aclose()
    with pytest.raises(RelayError):
        asyncio.run(go())


def test_cli_probe_refuses_to_touch_the_real_service_without_allow_real(project, monkeypatch, capsys):
    monkeypatch.setenv("RELAY_ROOT", str(project))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert main(["probe", "--stage", "single"]) == 2
    assert "--allow-real" in capsys.readouterr().err
    assert not (project / "results" / "probe_real.jsonl").exists()
