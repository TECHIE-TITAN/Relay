from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from test_analyze import make_run, row

from relay.artifacts import write_results_csv
from relay.dashboard_api import create_app

DOC = {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200000, "model": "m"}
EVENTS = [
    {"request_id": "r-0", "event": "arrive", "t_mono": 0.0},
    {"request_id": "r-0", "event": "slot_acquired", "t_mono": 0.1, "wait_ms": 100.0},
    {"request_id": "r-0", "event": "send", "t_mono": 0.2, "attempt": 1, "purpose": "draft"},
    {"request_id": "r-0", "event": "call_end", "t_mono": 0.7, "ok": True, "duration_ms": 500.0},
    {"request_id": "r-0", "event": "first_token", "t_mono": 0.8, "mode": "llm"},
    {"request_id": "r-0", "event": "done", "t_mono": 0.9, "status": "ok", "total_ms": 900.0, "first_token_ms": 800.0, "retries": 0,
     "tokens_in": 100, "tokens_out": 20, "retrieval_setting": "S2", "question_id": "q01", "answer_mode": "llm"},
    {"request_id": None, "event": "gateway_state", "t_mono": 0.5, "stage": "gateway", "inflight": 1, "queue_len": 0, "tpm_used": 120.0},
]


def client_for(tmp_path: Path, *, finished: bool = True, with_csv: bool = False) -> TestClient:
    rd = tmp_path / "results"
    extra = {"wall_end_utc": "2026-11-13T10:01:00+00:00"} if finished else {}
    make_run(rd, "steady_1", "steady", "stand-in", [row("r-0", "steady", "stand-in")], events=EVENTS, extra=extra)
    if with_csv:
        write_results_csv(tmp_path / "results.csv", [row("r-0", "steady", "stand-in"), row("r-1", "steady", "stand-in")])
    return TestClient(create_app(rd, root=tmp_path, documented_limits=DOC, prices={"in": 1.0, "out": 2.0}))


def test_runs_and_a_run_come_straight_from_the_manifest(tmp_path):
    c = client_for(tmp_path)
    runs = c.get("/api/runs").json()
    assert [r["run_id"] for r in runs] == ["steady_1"] and runs[0]["scenario"] == "steady" and runs[0]["live"] is False
    assert runs[0]["rows"] == 1 and runs[0]["graded"] is False
    m = c.get("/api/runs/steady_1").json()
    assert m["scenario"] == "steady" and m["source"] == "stand-in" and "summary" in m
    assert c.get("/api/runs/nope").status_code == 404
    assert c.get("/api/runs/..%2Fescape").status_code in (400, 404)
    assert c.get("/api/runs/bad id").status_code in (400, 404)


def test_a_run_without_an_end_time_is_reported_as_live(tmp_path):
    assert client_for(tmp_path, finished=False).get("/api/runs").json()[0]["live"] is True


def test_requests_and_one_trace_with_the_waiting_working_retrying_split(tmp_path):
    c = client_for(tmp_path)
    rows = c.get("/api/runs/steady_1/requests").json()
    assert len(rows) == 1 and rows[0]["waiting_ms"] == 100.0 and rows[0]["working_ms"] == 500.0 and rows[0]["retrying_ms"] == 0.0
    t = c.get("/api/runs/steady_1/requests/r-0").json()
    assert t["row"]["total_ms"] == 900.0 and [e["event"] for e in t["events"]][0] == "arrive"
    assert c.get("/api/runs/steady_1/requests/zzz").status_code == 404


def test_state_series_is_only_the_gateway_samples(tmp_path):
    s = client_for(tmp_path).get("/api/runs/steady_1/state").json()
    assert len(s) == 1 and s[0]["tpm_used"] == 120.0 and "request_id" not in s[0]


def test_stream_replays_a_finished_run_and_ends(tmp_path):
    body = client_for(tmp_path).get("/api/runs/steady_1/stream?poll_s=0.05").text
    assert body.count("data: {") >= len(EVENTS) and body.rstrip().endswith("event: end\ndata: {}")
    assert '"event": "done"' in body


def test_the_api_is_read_only_unless_simulations_are_switched_on(tmp_path):
    c = client_for(tmp_path)
    for method in ("post", "put", "delete", "patch"):
        assert getattr(c, method)("/api/runs").status_code == 405
    app = c.app
    assert all(r.methods <= {"GET", "HEAD"} for r in app.routes if hasattr(r, "methods") and r.path.startswith("/api"))
    assert c.post("/api/simulations", json={"pattern": "steady"}).status_code == 404       # not even a route


def test_runs_say_whether_they_are_evidence_or_a_simulation_and_whether_they_are_live_or_interrupted(tmp_path):
    import os, time
    rd = tmp_path / "results"
    make_run(rd, "steady_1", "steady", "stand-in", [row("r-0", "steady", "stand-in")], events=EVENTS, extra={"wall_end_utc": "2026-11-13T10:01:00+00:00"})
    make_run(rd, "sim_1", "sim_steady", "stand-in", [row("s-0", "sim_steady", "stand-in")], events=EVENTS, wall="2026-11-13T11:00:00+00:00")
    make_run(rd, "sim_old", "sim_burst", "stand-in", [row("o-0", "sim_burst", "stand-in")], events=EVENTS, wall="2026-11-13T09:00:00+00:00")
    stale = time.time() - 600
    os.utime(rd / "runs" / "sim_old" / "events.jsonl", (stale, stale))
    c = TestClient(create_app(rd, root=tmp_path, documented_limits=DOC, prices={}))
    by = {r["run_id"]: r for r in c.get("/api/runs").json()}
    assert by["steady_1"]["origin"] == "evidence" and by["steady_1"]["live"] is False and by["steady_1"]["interrupted"] is False
    assert by["sim_1"]["origin"] == "simulation" and by["sim_1"]["live"] is True                   # no end time, log just written
    assert by["sim_old"]["live"] is False and by["sim_old"]["interrupted"] is True                 # no end time, log stale for 10 minutes
    body = c.get("/api/runs/sim_old/stream?poll_s=0.05").text
    assert body.rstrip().endswith('event: end\ndata: {"interrupted": true}')                      # the stream says so instead of hanging


def test_rows_endpoint_filters_by_source_origin_and_scope(tmp_path):
    rd = tmp_path / "results"
    make_run(rd, "steady_old", "steady", "stand-in", [row("a", "steady", "stand-in")], wall="2026-11-13T09:00:00+00:00")
    make_run(rd, "steady_new", "steady", "stand-in", [row("b", "steady", "stand-in"), row("c", "steady", "stand-in")])
    make_run(rd, "quality_S2_1", "quality_S2", "real", [row("d", "quality_S2", "real")], kind="sequential")
    make_run(rd, "sim_1", "sim_steady", "stand-in", [row("e", "sim_steady", "stand-in")])
    c = TestClient(create_app(rd, root=tmp_path, documented_limits=DOC, prices={}))
    allr = c.get("/api/rows").json()                                     # default: evidence only, latest run per scenario
    assert allr["count"] == 3 and set(allr["runs"]) == {"steady_new", "quality_S2_1"}
    assert {r["source"] for r in c.get("/api/rows?source=real").json()["rows"]} == {"real"}
    assert c.get("/api/rows?source=stand-in").json()["count"] == 2
    assert c.get("/api/rows?scope=all").json()["count"] == 4             # steady_old comes back
    sim = c.get("/api/rows?origin=simulation").json()
    assert sim["count"] == 1 and sim["rows"][0]["scenario"] == "sim_steady" and sim["rows"][0]["run_id"] == "sim_1"
    assert c.get("/api/rows?source=bogus").status_code == 400
    r0 = allr["rows"][0]
    assert {"run_id", "scenario", "source", "first_token_ms", "total_ms", "status", "tokens_in"} <= set(r0)
    meta = allr["run_meta"]["steady_new"]                                # per-run facts the file of rows cannot hold
    assert meta["scenario"] == "steady" and meta["source"] == "stand-in" and meta["peak_queue_len"] == 3 and "peak_inflight" in meta


def test_run_events_endpoint_returns_only_run_level_events_by_name(tmp_path):
    rd = tmp_path / "results"
    ev = EVENTS + [{"request_id": None, "event": "pause_start", "t_mono": 5.0, "reason": "429"},
                   {"request_id": None, "event": "limit_changed", "t_mono": 6.0, "what": "tpm", "old": 8000, "new": 4000}]
    make_run(rd, "steady_1", "steady", "stand-in", [row("r-0", "steady", "stand-in")], events=ev)
    c = TestClient(create_app(rd, root=tmp_path, documented_limits=DOC, prices={}))
    names = [e["event"] for e in c.get("/api/runs/steady_1/events").json()]
    assert names == ["pause_start", "limit_changed"]                     # no per-request events, no gateway_state samples
    assert [e["event"] for e in c.get("/api/runs/steady_1/events?names=limit_changed").json()] == ["limit_changed"]


def test_experiments_come_from_results_csv_and_say_when_it_is_missing(tmp_path):
    assert client_for(tmp_path).get("/api/experiments").json()["available"] is False
    e = client_for(tmp_path / "second", with_csv=True).get("/api/experiments").json()
    assert e["available"] is True and e["rows"] == 2 and e["scenarios"]["steady"]["n"] == 2


def test_chunks_endpoint_returns_the_retrieved_passages_and_rejects_odd_ids(tmp_path):
    proc = tmp_path / "data" / "processed"
    proc.mkdir(parents=True)
    rec = {"chunk_id": "attention:S2:0006", "paper_key": "attention", "setting_id": "S2", "page_start": 3, "page_end": 3,
           "printed_start": "3", "printed_end": "3", "section": "3.1 Encoder and Decoder Stacks", "text": "x" * 1000, "approx_tokens": 250}
    proc.joinpath("chunks_S2.jsonl").write_text(json.dumps(rec) + "\n" + json.dumps({**rec, "chunk_id": "moshi:S2:0001", "paper_key": "moshi", "text": "short"}) + "\n", encoding="utf-8")
    c = TestClient(create_app(tmp_path / "results", root=tmp_path, documented_limits=DOC, prices={}))
    got = c.get("/api/chunks?ids=attention:S2:0006,attention:S2:0099,moshi:S2:0001").json()["chunks"]
    assert [x["chunk_id"] for x in got] == ["attention:S2:0006", "attention:S2:0099", "moshi:S2:0001"]
    assert got[0]["paper"] == "attention" and got[0]["pdf_pages"] == [3, 3] and got[0]["printed_pages"] == ["3", "3"] and got[0]["section"].startswith("3.1")
    assert len(got[0]["text"]) == 900 and got[0]["clipped"] is True                  # long passages are clipped, and it says so
    assert got[1] == {"chunk_id": "attention:S2:0099", "missing": True} and got[2]["text"] == "short" and got[2]["clipped"] is False
    for bad in ("../etc/passwd", "attention:S2", "attention:../../x:0001", "a b:S2:0001"):
        assert c.get("/api/chunks", params={"ids": bad}).status_code == 400
    assert c.get("/api/chunks?ids=attention:S9:0001").json()["chunks"][0]["missing"] is True      # no such setting file: missing, not an error


def test_experiments_carry_model_ttft_grade_detail_and_retrieval_scores(tmp_path):
    rd = tmp_path / "results"
    mft = [{"request_id": f"q-{i}", "event": "model_first_token", "t_mono": float(i), "ttft_ms": v} for i, v in enumerate((100.0, 200.0, 300.0))]
    paths = make_run(rd, "quality_S2_1", "quality_S2", "real", [row("q-0", "quality_S2", "real", grade="correct")], kind="sequential", events=mft)
    (paths.root / "grades.csv").write_text("request_id,answer_mode,detail_grade\nq-0,llm,correct\nq-1,extractive,incorrect\nq-2,refusal,\n", encoding="utf-8")
    write_results_csv(tmp_path / "results.csv", [row("q-0", "quality_S2", "real", grade="correct")])
    (tmp_path / "results.sources.json").write_text(json.dumps({"runs_used": [{"scenario": "quality_S2", "run_id": "quality_S2_1", "rows": 1},
                                                                           {"scenario": "steady", "run_id": "../escape", "rows": 1}]}), encoding="utf-8")
    ev = rd / "retrieval_eval"
    ev.mkdir()
    ev.joinpath("retrieval_eval_20261112T000000Z.json").write_text(json.dumps({"per_question": [
        {"setting": "S2", "id": "q01", "answerable": True, "abstain": False, "evidence_recall": 1.0, "hit_all": True, "context_tokens_approx": 1000},
        {"setting": "S4", "id": "q01", "answerable": True, "abstain": False, "evidence_recall": 0.5, "hit_all": False, "context_tokens_approx": 1900},
    ]}), encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "relay.yaml").write_text("retrieval:\n  settings:\n    S2: {chunk_tokens: 384, overlap_tokens: 48, k: 4, fetch_k: 4, rerank: none}\n", encoding="utf-8")
    e = TestClient(create_app(rd, root=tmp_path, documented_limits=DOC, prices={})).get("/api/experiments").json()
    assert e["settings"]["S2"]["chunk_tokens"] == 384 and e["settings"]["S2"]["rerank"] == "none"
    assert e["model_ttft"] == {"quality_S2": {"n": 3, "p50": 200.0, "p95": 290.0}}                  # unsafe run ids in the sources file are skipped
    assert e["grade_detail"] == {"quality_S2": {"llm:correct": 1, "extractive:incorrect": 1}}        # ungraded rows are not counted
    assert e["retrieval"]["file"] == "retrieval_eval_20261112T000000Z.json"
    assert e["retrieval"]["settings"]["S2"]["recall_sum"] == 1.0 and e["retrieval"]["settings"]["S4"]["hit_all"] == 0


def test_limits_keep_the_provenance_labels_and_never_show_the_org_id(tmp_path):
    rd = tmp_path / "results"
    rd.mkdir(parents=True)
    (rd / "probe_real.jsonl").write_text("\n".join(json.dumps(d) for d in [
        {"source": "real", "stage": "single", "ok": True, "headers": {"x-ratelimit-limit-tokens": "8000", "x-ratelimit-limit-requests": "1000"}},
        {"source": "real", "stage": "limit", "ok": False, "error_kind": "rate_limit_minute", "status_code": 429, "retry_after_s": 2.0,
         "message": "Rate limit reached in organization org_01m77rec8vesj8v4r411tgqwsp on requests per minute (RPM): Limit 30",
         "headers": {"retry-after": "2"}},
        {"source": "stand-in", "stage": "limit", "ok": False, "error_kind": "rate_limit_minute", "message": "test hook row"},
    ]) + "\n", encoding="utf-8")
    c = TestClient(create_app(rd, root=tmp_path, documented_limits=DOC, prices={}))
    lim = c.get("/api/limits").json()
    assert lim["documented"]["provenance"].startswith("DOCUMENTED") and lim["documented"]["tpm"] == 8000
    assert lim["observed_reported"]["limit_tokens_per_minute"] == "8000"
    assert lim["observed_triggered"]["provenance"].startswith("OBSERVED-TRIGGERED") and lim["observed_triggered"]["retry_after_s"] == 2.0
    assert "org_01m77" not in json.dumps(lim) and "org_[REDACTED]" in lim["observed_triggered"]["message"]
    assert lim["simulated"]["provenance"].startswith("SIMULATED") and lim["probe_calls_by_stage"] == {"single": 1, "limit": 1}


def test_ledger_reports_the_file_or_says_it_is_unavailable(tmp_path):
    c = client_for(tmp_path)
    assert c.get("/api/ledger").json() == {"available": False}
    (tmp_path / "results" / "ledger_x.json").write_text(json.dumps({"date": "d", "tokens": 5, "requests": 1}), encoding="utf-8")
    assert c.get("/api/ledger").json()["tokens"] == 5


def test_the_dashboard_module_cannot_reach_the_provider():
    src = (Path(__file__).resolve().parents[1] / "src" / "relay" / "dashboard_api.py").read_text(encoding="utf-8")
    for forbidden in ("llm_client", "get_api_key", "GROQ", "import httpx", ".gateway import", ".runner import", ".probe import"):
        assert forbidden not in src, forbidden
