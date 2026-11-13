from __future__ import annotations

import hashlib
import json
from pathlib import Path

from relay.analyze import analyze, probe_rows, select_runs, slowest_request
from relay.artifacts import RESULT_COLUMNS, STATUSES, read_results_csv, run_paths, write_manifest, write_results_csv

PRICES = {"in": 1.0, "out": 2.0}      # USD per million tokens, chosen so the arithmetic is easy to check


def row(rid, scenario, source, status="ok", ft="10.0", total="100.0", retries=0, tin=100, tout=20, setting="S2", grade="",
        start="2026-11-13T10:00:00.000+00:00"):
    return dict(request_id=rid, scenario=scenario, source=source, start_time=start, first_token_ms=ft, total_ms=total,
                status=status, retries=retries, tokens_in=tin, tokens_out=tout, retrieval_setting=setting, answer_grade=grade)


def make_run(results_dir: Path, run_id, scenario, source, rows, *, wall="2026-11-13T10:00:00+00:00", kind="open_loop",
             peak=3, events=(), extra=None):
    paths = run_paths(results_dir, run_id)
    paths.root.mkdir(parents=True)
    write_manifest(paths.manifest, {"run_id": run_id, "scenario": scenario, "source": source, "kind": kind,
                                    "wall_start_utc": wall, "summary": {"peak_queue_len": peak}, **(extra or {})})
    write_results_csv(paths.results, rows)
    paths.events.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return paths


def run_analyze(tmp_path, probe=None):
    out = analyze(tmp_path / "results", tmp_path / "results.csv", tmp_path / "report" / "tables.md",
                  probe_path=probe, prices=PRICES, settings_desc={"S1": "256 tok, k=3, no rerank", "S2": "384 tok, k=4, no rerank",
                                                                  "S3": "512 tok, k=4, mmr"})
    return out, read_results_csv(tmp_path / "results.csv"), (tmp_path / "report" / "tables.md").read_text(encoding="utf-8")


def test_merge_keeps_the_latest_run_per_scenario_and_excludes_pilots(tmp_path):
    rd = tmp_path / "results"
    make_run(rd, "steady_old", "steady", "stand-in", [row("old-0", "steady", "stand-in")], wall="2026-11-13T09:00:00+00:00")
    make_run(rd, "steady_new", "steady", "stand-in", [row("new-0", "steady", "stand-in"), row("new-1", "steady", "stand-in")])
    make_run(rd, "pilot", "quality_measure", "real", [row("p-0", "quality_measure", "real")])
    assert set(select_runs(rd)) == {"steady"} and select_runs(rd)["steady"][0] == "steady_new"
    res, rows, _ = run_analyze(tmp_path)
    assert list(rows[0].keys()) == list(RESULT_COLUMNS)
    assert [r["request_id"] for r in rows] == ["new-0", "new-1"]                  # no old run, no pilot
    assert {r["status"] for r in rows} <= set(STATUSES)


def _tree_hashes(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def test_analyze_never_modifies_run_folders_and_writes_a_traceable_sources_file(tmp_path):
    rd = tmp_path / "results"
    make_run(rd, "steady_old", "steady", "stand-in", [row("steady_old-0000", "steady", "stand-in")], wall="2026-11-13T09:00:00+00:00")
    make_run(rd, "steady_new", "steady", "stand-in",
             [row("steady_new-0000", "steady", "stand-in"), row("steady_new-0001", "steady", "stand-in")])
    make_run(rd, "pilot", "quality_measure", "real", [row("pilot-0000", "quality_measure", "real")])
    before = _tree_hashes(rd / "runs")
    res, rows, _ = run_analyze(tmp_path)
    assert _tree_hashes(rd / "runs") == before                         # every original run folder is byte-for-byte unchanged
    src = json.loads((tmp_path / "results.sources.json").read_text(encoding="utf-8"))
    assert [r["run_id"] for r in src["runs_used"]] == ["steady_new"]
    reasons = {x["run_id"]: x["reason"] for x in src["runs_left_out"]}
    assert "superseded" in reasons["steady_old"] and "pilot" in reasons["pilot"]
    assert src["rows"] == len(rows) == src["rows_from_runs"] + src["rows_from_probe"] == 2
    assert src["results_csv_sha256"] == hashlib.sha256((tmp_path / "results.csv").read_bytes()).hexdigest()
    used = src["runs_used"][0]
    assert used["results_csv_sha256"] == hashlib.sha256((rd / "runs" / "steady_new" / "results.csv").read_bytes()).hexdigest()
    assert used["manifest_sha256"] == hashlib.sha256((rd / "runs" / "steady_new" / "manifest.json").read_bytes()).hexdigest()
    assert all(r["request_id"].startswith(used["run_id"]) for r in rows)    # a row names the folder it came from


def test_dashboard_simulations_are_never_merged_into_the_evidence(tmp_path):
    rd = tmp_path / "results"
    make_run(rd, "steady_1", "steady", "stand-in", [row("steady_1-0000", "steady", "stand-in")])
    make_run(rd, "sim_steady_x", "sim_steady", "stand-in", [row("sim_steady_x-0000", "sim_steady", "stand-in")], wall="2026-11-13T12:00:00+00:00")
    assert set(select_runs(rd)) == {"steady"}
    res, rows, _ = run_analyze(tmp_path)
    assert [r["request_id"] for r in rows] == ["steady_1-0000"]
    src = json.loads((tmp_path / "results.sources.json").read_text(encoding="utf-8"))
    assert [x["run_id"] for x in src["runs_left_out"]] == ["sim_steady_x"] and "dashboard" in src["runs_left_out"][0]["reason"]


def test_probe_calls_become_real_rows_with_the_429_as_rate_limited(tmp_path):
    p = tmp_path / "probe_real.jsonl"
    p.write_text("\n".join(json.dumps(d) for d in [
        {"source": "real", "stage": "latency", "ts": "2026-11-13T18:50:36.394+00:00", "ok": True, "ttft_ms": 581.9, "total_ms": 600.3,
         "tokens_in": 1477, "tokens_out": 29, "error_kind": None},
        {"source": "real", "stage": "limit", "ts": "2026-11-13T19:00:00.000+00:00", "ok": False, "ttft_ms": None, "total_ms": 170.0,
         "tokens_in": 0, "tokens_out": 0, "error_kind": "rate_limit_minute"},
        {"source": "simulated", "stage": "limit", "ts": "2026-11-13T19:00:00.000+00:00", "ok": True},       # a test-hook row: ignored
    ]) + "\n", encoding="utf-8")
    rows = probe_rows(p)
    assert [(r["scenario"], r["status"], r["source"]) for r in rows] == [("probe_latency", "ok", "real"), ("probe_limit", "rate_limited", "real")]
    assert rows[0]["first_token_ms"] == 581.9 and rows[1]["first_token_ms"] == "" and rows[1]["retrieval_setting"] == ""
    res, merged, _ = run_analyze(tmp_path, probe=p)
    assert {r["scenario"] for r in merged} == {"probe_latency", "probe_limit"} and res["n_rows"] == 2


def test_table_figures_are_computed_from_the_merged_file(tmp_path):
    rows = [row(f"s-{i}", "steady", "stand-in", ft=str(10 * (i + 1)), total=str(100 * (i + 1)), retries=1 if i == 0 else 0,
                start=f"2026-11-13T10:00:{i:02d}.000+00:00") for i in range(4)]
    rows += [row("s-4", "steady", "stand-in", status="rate_limited", ft="", total="50.0", tin=0, tout=0,
                 start="2026-11-13T10:00:04.000+00:00"),
             row("s-5", "steady", "stand-in", status="timeout", ft="", total="2000.0", tin=0, tout=0,
                 start="2026-11-13T10:00:05.000+00:00")]
    make_run(tmp_path / "results", "steady_1", "steady", "stand-in", rows, peak=7)
    res, merged, md = run_analyze(tmp_path)
    s = res["scenarios"]["steady"]
    assert s["n"] == 6 and s["by_status"] == {"ok": 4, "rate_limited": 1, "timeout": 1, "error": 0}
    assert s["latency"]["ft"]["p50"] == 25.0 and s["latency"]["total"]["p50"] == 250.0        # over the 4 ok requests, type 7
    assert s["latency"]["total"]["p99"] is None                                                 # fewer than 100 requests
    assert abs(s["retries_per_request"] - 1 / 6) < 1e-9
    assert s["tokens_per_answer"] == 120.0                                                      # (100 + 20) per ok answer
    assert abs(s["cost_per_1000"] - (4 * 100 * 1.0 + 4 * 20 * 2.0) / 1e6 / 4 * 1000) < 1e-9
    assert abs(s["offered_per_min"] - 6 / 5 * 60) < 1e-9 and abs(s["duration_s"] - 7.0) < 1e-6     # 5 s of arrivals; last ends at 7 s
    assert "| steady | stand-in | 6 |" in md and "66.7" in md and "n<100" in md and "| 7 |" in md
    assert "not measured" in md                                                                 # no grades on a stand-in run


def test_quality_table_uses_the_four_grades_and_flags_ungraded_or_incomplete_runs(tmp_path):
    grades = ["correct", "wrong", "refused_correctly", "refused_wrongly"]
    rows = [row(f"q-{i}", "quality_S2", "real", grade=g, start=f"2026-11-13T19:00:{i:02d}.000+00:00") for i, g in enumerate(grades)]
    mft = [{"request_id": f"q-{i}", "event": "model_first_token", "t_mono": float(i), "ttft_ms": v} for i, v in enumerate((100.0, 200.0, 300.0))]
    make_run(tmp_path / "results", "q2", "quality_S2", "real", rows, kind="sequential", extra={"grading": {"n_graded": 4}}, events=mft)
    make_run(tmp_path / "results", "q1", "quality_S1", "real",
             [row("a-0", "quality_S1", "real", setting="S1"), row("a-1", "quality_S1", "real", setting="S1")],
             kind="sequential", extra={"incomplete": True, "stop": {"reason": "cap_reached", "not_run": ["q05", "q06"]}})
    res, _, md = run_analyze(tmp_path)
    t = res["scenarios"]["quality_S2"]
    assert t["grades"] == {"correct": 1, "wrong": 1, "refused_correctly": 1, "refused_wrongly": 1}
    assert "| S2: 384 tok, k=4, no rerank | 4 | 25.0 | 25.0 | 25.0 | 25.0 |" in md
    assert "| S1: 256 tok, k=3, no rerank | 2 | not measured | not measured | not measured | not measured |" in md
    assert any("quality_S1" in w and "INCOMPLETE" in w and "2 questions" in w for w in res["warnings"])
    assert any("quality_S1" in w and "not been graded" in w for w in res["warnings"])
    assert not any("quality_S2" in w for w in res["warnings"])
    assert "(closed loop)" in md
    assert res["model_ttft"]["quality_S2"] == {"n": 3, "p50": 200.0, "p95": 290.0}      # from events, type 7
    assert "| 200.0 / 290.0 |" in md and res["model_ttft"]["quality_S1"]["n"] == 0


def test_slowest_by_retrying_picks_the_request_that_retried_and_none_when_nobody_did():
    def req(rid, total, retry_ms):
        ev = [{"request_id": rid, "event": "arrive", "t_mono": 0.0},
              {"request_id": rid, "event": "send", "t_mono": 0.0},
              {"request_id": rid, "event": "call_end", "t_mono": 0.1, "ok": True, "duration_ms": 100.0}]
        if retry_ms:
            ev.append({"request_id": rid, "event": "retry_sleep_start", "t_mono": 0.1, "sleep_ms": retry_ms})
            ev.append({"request_id": rid, "event": "retry_sleep_end", "t_mono": 0.1 + retry_ms / 1000})
        ev.append({"request_id": rid, "event": "done", "t_mono": total / 1000, "status": "ok", "total_ms": total,
                   "first_token_ms": total, "retries": 1 if retry_ms else 0, "tokens_in": 1, "tokens_out": 1,
                   "retrieval_setting": "S2", "question_id": "q01", "answer_mode": "llm"})
        return ev
    man = {"scenario": "x", "source": "stand-in", "wall_start_utc": "2026-11-13T10:00:00+00:00"}
    ev = req("slow-no-retry", 9000.0, 0) + req("retried", 3000.0, 500.0)
    assert slowest_request(ev, man)["request_id"] == "slow-no-retry"                          # by total time
    assert slowest_request(ev, man, by="retrying")["request_id"] == "retried"                 # by time spent retrying
    assert slowest_request(req("a", 1000.0, 0), man, by="retrying") is None


def test_slowest_request_split_says_whether_the_delay_came_before_the_first_token():
    ev = [
        {"request_id": "s-0", "event": "arrive", "t_mono": 0.0},
        {"request_id": "s-0", "event": "slot_acquired", "t_mono": 0.1, "wait_ms": 100.0},
        {"request_id": "s-0", "event": "send", "t_mono": 0.2, "attempt": 1, "purpose": "draft"},
        {"request_id": "s-0", "event": "call_end", "t_mono": 0.5, "ok": False, "error_kind": "server_error", "duration_ms": 300.0},
        {"request_id": "s-0", "event": "retry_sleep_start", "t_mono": 0.5, "sleep_ms": 500.0},
        {"request_id": "s-0", "event": "retry_sleep_end", "t_mono": 1.0},
        {"request_id": "s-0", "event": "send", "t_mono": 1.0, "attempt": 2, "purpose": "draft"},
        {"request_id": "s-0", "event": "call_end", "t_mono": 1.4, "ok": True, "duration_ms": 400.0},
        {"request_id": "s-0", "event": "first_token", "t_mono": 1.45, "mode": "llm"},
        {"request_id": "s-0", "event": "done", "t_mono": 1.5, "status": "ok", "total_ms": 1500.0, "first_token_ms": 1450.0,
         "retries": 1, "tokens_in": 100, "tokens_out": 20, "retrieval_setting": "S2", "question_id": "q01", "answer_mode": "llm"},
    ]
    s = slowest_request(ev, {"scenario": "x", "source": "stand-in", "wall_start_utc": "2026-11-13T10:00:00+00:00"})
    assert s["total_ms"] == 1500.0 and s["first_token_ms"] == 1450.0 and s["after_first_token_ms"] == 50.0
    assert (s["waiting_ms"], s["working_ms"], s["retrying_ms"]) == (100.0, 400.0, 800.0)      # 300 failed call + 500 back-off
    assert [e["event"] for e in s["timeline"]][0] == "arrive" and s["timeline"][-1]["event"] == "done"
