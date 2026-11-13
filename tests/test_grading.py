from __future__ import annotations

import json

import pytest

from relay.artifacts import RESULT_COLUMNS, read_manifest, read_results_csv, run_paths, write_manifest, write_results_csv
from relay.cli import main
from relay.eval_retrieval import Gold, Question
from relay.grading import (
    CSV_GRADES, DETAIL_TO_CSV, allowed_details, apply_grades, build_sheet, derive_grades, read_sheet, write_sheet,
)
from relay.schema import RelayError

QS = {
    "q01": Question("q01", "How many identical layers?", True, "single-fact", (Gold("attention", (3,), ("N = 6",)),)),
    "q16": Question("q16", "GPT-4 latency on Moshi's hardware?", False, "unanswerable"),
    "q04": Question("q04", "Multi-step question?", True, "multi-step", (Gold("attention", (3,), ("x",)),)),
}
MANIFEST = {"source": "real", "scenario": "quality_S2", "stop": None, "incomplete": False}


def events_for(specs):
    """specs: (request_id, question_id, status, answer_mode, answer)"""
    ev = []
    for rid, qid, status, mode, answer in specs:
        ev.append({"request_id": rid, "event": "arrive"})
        if answer:
            ev.append({"request_id": rid, "event": "first_token", "answer": answer, "mode": mode})
        ev.append({"request_id": rid, "event": "done", "question_id": qid, "status": status, "answer_mode": mode,
                   "retrieval_setting": "S2"})
    return ev


EVENTS = events_for([
    ("r0", "q01", "ok", "llm", "Six identical layers."),
    ("r1", "q16", "ok", "refusal", "The three papers do not contain enough information to answer this question."),
    ("r2", "q04", "ok", "refusal", "The three papers do not contain enough information to answer this question."),
    ("r3", "q04", "error", "none", ""),
])


def test_required_answer_grade_values_and_mapping_are_exactly_the_briefs_four():
    assert CSV_GRADES == ("correct", "wrong", "refused_correctly", "refused_wrongly")
    assert set(DETAIL_TO_CSV.values()) == set(CSV_GRADES)
    assert DETAIL_TO_CSV == {"correct": "correct", "partial": "wrong", "incorrect": "wrong",
                             "answered_unanswerable": "wrong", "wrong_refusal": "refused_wrongly",
                             "correct_refusal": "refused_correctly"}


def test_sheet_has_one_row_per_finished_request_with_the_answer_the_user_saw():
    rows = build_sheet(MANIFEST, EVENTS, QS)
    assert [r["request_id"] for r in rows] == ["r0", "r1", "r2", "r3"]
    assert rows[0]["answer"] == "Six identical layers." and rows[0]["answerable"] == "yes" and rows[0]["gold"].startswith("attention pdf p.3")
    assert rows[1]["answerable"] == "no" and "unanswerable" in rows[1]["gold"]
    assert rows[3]["status"] == "error" and rows[3]["answer"] == "" and all(r["detail_grade"] == "" for r in rows)


def test_only_real_non_pilot_runs_can_be_graded():
    for bad in ({**MANIFEST, "source": "stand-in"}, {**MANIFEST, "scenario": "quality_measure"}):
        with pytest.raises(RelayError):
            build_sheet(bad, EVENTS, QS)


def test_allowed_grades_follow_question_type_status_and_answer_mode():
    assert allowed_details(True, "ok", "llm") == ("correct", "partial", "incorrect")
    assert allowed_details(True, "ok", "extractive") == ("correct", "partial", "incorrect")
    assert allowed_details(True, "ok", "refusal") == ("wrong_refusal",)
    assert allowed_details(False, "ok", "refusal") == ("correct_refusal",)
    assert allowed_details(False, "ok", "extractive") == ("answered_unanswerable",)
    assert allowed_details(True, "error", "none") == () and allowed_details(True, "rate_limited", "none") == ()


def graded_sheet(**overrides):
    rows = build_sheet(MANIFEST, EVENTS, QS)
    given = {"r0": "correct", "r1": "correct_refusal", "r2": "wrong_refusal", **overrides}
    for r in rows:
        r["detail_grade"] = given.get(r["request_id"], "")
    return rows


def test_derive_maps_each_detail_to_the_required_value_and_skips_failed_requests():
    d = derive_grades(graded_sheet())
    assert d == {"r0": ("correct", "correct"), "r1": ("correct_refusal", "refused_correctly"),
                 "r2": ("wrong_refusal", "refused_wrongly")}
    assert derive_grades(graded_sheet(r0="partial"))["r0"] == ("partial", "wrong")


@pytest.mark.parametrize("override, fragment", [
    ({"r1": "answered_unanswerable"}, "does not fit"),      # it refused: cannot be 'answered'
    ({"r2": "correct"}, "does not fit"),                   # refused an answerable question: only wrong_refusal
    ({"r0": "correct_refusal"}, "does not fit"),           # an answer is not a refusal
    ({"r0": "perfect"}, "unknown grade"),
    ({"r3": "incorrect"}, "not gradable"),                 # the failed request has no answer to grade
])
def test_inconsistent_or_unknown_grades_are_rejected(override, fragment):
    with pytest.raises(RelayError, match=fragment):
        derive_grades(graded_sheet(**override))


def test_missing_grades_are_rejected_unless_partial_is_allowed():
    rows = graded_sheet(r0="")
    with pytest.raises(RelayError, match="no grade yet"):
        derive_grades(rows)
    assert "r0" not in derive_grades(rows, allow_partial=True)


def write_run_files(path, rows):
    write_results_csv(path, rows)


def result_rows():
    base = dict(scenario="quality_S2", source="real", start_time="2026-11-13T19:00:00.000+00:00", first_token_ms="500.0",
                total_ms="520.0", status="ok", retries="0", tokens_in="1593", tokens_out="40", retrieval_setting="S2",
                answer_grade="")
    return [{**base, "request_id": rid, **({"status": "error", "first_token_ms": "", "tokens_in": "0", "tokens_out": "0"} if rid == "r3" else {})}
            for rid in ("r0", "r1", "r2", "r3")]


def test_apply_writes_only_the_four_values_keeps_the_schema_and_a_backup(tmp_path):
    p = tmp_path / "results.csv"
    write_run_files(p, result_rows())
    block = apply_grades(p, graded_sheet())
    rows = read_results_csv(p)
    assert list(rows[0].keys()) == list(RESULT_COLUMNS)
    assert {r["request_id"]: r["answer_grade"] for r in rows} == {
        "r0": "correct", "r1": "refused_correctly", "r2": "refused_wrongly", "r3": ""}
    assert {r["answer_grade"] for r in rows} - {""} <= set(CSV_GRADES)
    assert rows[0]["tokens_in"] == "1593" and rows[3]["status"] == "error"          # nothing else changed
    backup = tmp_path / "results.ungraded.csv"
    assert backup.is_file() and all(r["answer_grade"] == "" for r in read_results_csv(backup))
    assert block["n_graded"] == 3 and block["answer_grade_counts"] == {"correct": 1, "refused_correctly": 1, "refused_wrongly": 1}
    assert block["by_mode_and_detail"]["llm:correct"] == 1 and block["n_ungraded_ok"] == 0
    # a second apply must not overwrite the original backup
    apply_grades(p, graded_sheet(r0="partial"))
    assert all(r["answer_grade"] == "" for r in read_results_csv(backup))
    assert read_results_csv(p)[0]["answer_grade"] == "wrong"


def test_prefill_refusals_fills_only_refusals_and_marks_them():
    rows = build_sheet(MANIFEST, EVENTS, QS, prefill_refusals=True)
    assert [r["detail_grade"] for r in rows] == ["", "correct_refusal", "wrong_refusal", ""]    # the answer and the failure stay blank
    assert "auto" in rows[1]["note"] and "auto" in rows[2]["note"] and rows[0]["note"] == ""
    assert all(r["detail_grade"] == "" for r in build_sheet(MANIFEST, EVENTS, QS))                # off by default


def test_proposals_fill_only_blank_cells_are_marked_and_are_reported_as_assistant_proposed(tmp_path):
    from relay.grading import merge_proposals
    rows = graded_sheet(r0="")                                      # r1, r2 are set; r0 is blank; r3 is a failed request
    rows[0]["detail_grade"] = ""
    prop = tmp_path / "p.csv"
    prop.write_text("request_id,detail_grade,note\nr0,correct,\"cites p.3, N = 6\"\nr1,answered_unanswerable,\"must not overwrite\"\n",
                    encoding="utf-8")
    merged, n = merge_proposals(rows, prop)
    assert n == 1 and merged[0]["detail_grade"] == "correct" and "assistant-proposed: cites p.3, N = 6" in merged[0]["note"]
    assert merged[1]["detail_grade"] == "correct_refusal"            # an existing grade is never overwritten
    p = tmp_path / "results.csv"
    write_run_files(p, result_rows())
    block = apply_grades(p, merged)
    assert "1 proposed by the assistant" in block["graded_by"] and "responsible" in block["graded_by"]
    assert "author" in block["graded_by"]
    bad = tmp_path / "bad.csv"
    bad.write_text("request_id,detail_grade,note\nzzz,correct,x\n", encoding="utf-8")
    with pytest.raises(RelayError, match="not in the sheet"):
        merge_proposals(rows, bad)


def test_a_cap_stop_is_flagged_on_the_request_where_it_happened():
    m = {**MANIFEST, "incomplete": True, "stop": {"reason": "cap_reached", "after_request": "r2", "not_run": ["q05"]}}
    rows = build_sheet(m, EVENTS, QS)
    assert "CAP REACHED" in rows[2]["note"] and rows[0]["note"] == ""


# ---------------------------------------------------------------- the CLI, on a tiny project

QUESTIONS_YAML = """\
version: 1
questions:
  - {id: q01, text: "How many identical layers are in the encoder stack?", answerable: true, category: single-fact, gold: [{paper: attention, pages: [3]}]}
  - {id: q16, text: "Who won the football match yesterday?", answerable: false, category: unanswerable}
"""


def make_run(project, run_id, manifest):
    (project / "data").mkdir(exist_ok=True)
    (project / "data" / "questions.yaml").write_text(QUESTIONS_YAML, encoding="utf-8")
    paths = run_paths(project / "results", run_id)
    paths.root.mkdir(parents=True)
    write_manifest(paths.manifest, manifest)
    ev = events_for([("a0", "q01", "ok", "llm", "Six layers."),
                     ("a1", "q16", "ok", "refusal", "The three papers do not contain enough information to answer this question.")])
    paths.events.write_text("\n".join(json.dumps(e) for e in ev) + "\n", encoding="utf-8")
    rows = [dict(request_id=r, scenario=manifest["scenario"], source="real", start_time="t", first_token_ms="1", total_ms="2",
                 status="ok", retries="0", tokens_in="1", tokens_out="1", retrieval_setting="S2", answer_grade="") for r in ("a0", "a1")]
    write_results_csv(paths.results, rows)
    return paths


def test_cli_grade_sheet_then_apply_grades_end_to_end(project, monkeypatch, capsys):
    monkeypatch.setenv("RELAY_ROOT", str(project))
    paths = make_run(project, "study_run", {**MANIFEST, "run_id": "study_run"})
    assert main(["grade-sheet", "--run", "study_run"]) == 0
    sheet = paths.root / "grades.csv"
    assert sheet.is_file()
    assert main(["grade-sheet", "--run", "study_run"]) == 2                        # will not clobber a sheet you may have filled in
    assert main(["apply-grades", "--run", "study_run"]) == 2                       # ungraded answers are refused
    assert "no grade yet" in capsys.readouterr().err
    rows = read_sheet(sheet)
    rows[0]["detail_grade"], rows[1]["detail_grade"] = "correct", "correct_refusal"
    write_sheet(sheet, rows)
    assert main(["apply-grades", "--run", "study_run"]) == 0
    graded = {r["request_id"]: r["answer_grade"] for r in read_results_csv(paths.results)}
    assert graded == {"a0": "correct", "a1": "refused_correctly"}
    assert read_manifest(paths.manifest)["grading"]["n_graded"] == 2


def test_cli_refuses_pilot_and_stand_in_runs_and_never_touches_their_files(project, monkeypatch):
    monkeypatch.setenv("RELAY_ROOT", str(project))
    pilot = make_run(project, "pilot_run", {**MANIFEST, "scenario": "quality_measure"})
    before = pilot.results.read_bytes()
    assert main(["grade-sheet", "--run", "pilot_run"]) == 2
    assert main(["apply-grades", "--run", "pilot_run"]) == 2
    assert pilot.results.read_bytes() == before and not (pilot.root / "grades.csv").exists()
    standin = make_run(project, "standin_run", {**MANIFEST, "source": "stand-in"})
    assert main(["grade-sheet", "--run", "standin_run"]) == 2 and not (standin.root / "grades.csv").exists()
    assert main(["grade-sheet", "--run", "missing_run"]) == 2
