"""Manual grading of a real quality run: export a sheet, the author fills it in, merge it into results.csv.

Nothing here invents a grade. The author writes one *detailed* grade per released answer in the sheet; the merge
step derives the brief's required `answer_grade` value from it and refuses inconsistent combinations.

Detailed grade (sheet)  ->  answer_grade (results.csv, the brief's four values)
  correct                    correct
  partial                    wrong        (strict: anything short of fully correct is wrong; the detail keeps "partial")
  incorrect                  wrong
  answered_unanswerable      wrong        (an answer where the papers have none)
  wrong_refusal              refused_wrongly   (refused an answerable question)
  correct_refusal            refused_correctly (refused an unanswerable question)

Requests that released no answer (status not ok) are not gradable and keep an empty answer_grade.
Stand-in runs and the pilot runs are refused: no quality number may come from them.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifacts import read_results_csv, write_results_csv
from .schema import RelayError

CSV_GRADES = ("correct", "wrong", "refused_correctly", "refused_wrongly")
DETAIL_TO_CSV = {
    "correct": "correct",
    "partial": "wrong",
    "incorrect": "wrong",
    "answered_unanswerable": "wrong",
    "wrong_refusal": "refused_wrongly",
    "correct_refusal": "refused_correctly",
}
PROTECTED_SCENARIOS = frozenset({"quality_measure"})      # the three pilot runs: kept as evidence, never graded
SHEET_COLUMNS = ("request_id", "question_id", "category", "answerable", "status", "answer_mode", "retrieval_setting",
                 "question", "gold", "answer", "detail_grade", "note")


def check_gradable_run(manifest: Mapping[str, Any]) -> None:
    if manifest.get("source") != "real":
        raise RelayError("only runs against the real service are graded (a stand-in's replies are templated)")
    if manifest.get("scenario") in PROTECTED_SCENARIOS:
        raise RelayError(f"scenario '{manifest.get('scenario')}' is a pilot and is kept untouched; it is not graded")


def allowed_details(answerable: bool, status: str, answer_mode: str) -> tuple:
    """Which detailed grades are valid for one request."""
    if status != "ok" or answer_mode not in ("llm", "extractive", "refusal"):
        return ()
    if answer_mode == "refusal":
        return ("wrong_refusal",) if answerable else ("correct_refusal",)
    return ("correct", "partial", "incorrect") if answerable else ("answered_unanswerable",)


def gold_summary(question: Any) -> str:
    if not question.answerable:
        return "(unanswerable: the papers do not contain the answer)"
    parts = []
    for g in question.gold:
        where = f"{g.paper} pdf p.{','.join(str(p) for p in g.pages)}" if g.pages else g.paper
        ev = "; ".join(g.evidence)
        parts.append(f"{where}: {ev}" if ev else where)
    return " | ".join(parts)


def build_sheet(manifest: Mapping[str, Any], events: Sequence[Mapping[str, Any]], questions: Mapping[str, Any]) -> list:
    """One row per finished request, in arrival order, with the answer the user actually saw."""
    check_gradable_run(manifest)
    per: dict = {}
    order: list = []
    for e in events:
        rid = e.get("request_id")
        if not rid:
            continue
        if rid not in per:
            per[rid] = {}
            order.append(rid)
        per[rid].setdefault(e["event"], e)
    stop = manifest.get("stop") or {}
    rows = []
    for rid in order:
        evs = per[rid]
        done = evs.get("done")
        if done is None:
            continue
        qid = done.get("question_id")
        q = questions.get(qid)
        if q is None:
            raise RelayError(f"{rid}: question '{qid}' is not in the questions file")
        note = ""
        if stop.get("reason") == "cap_reached" and stop.get("after_request") == rid:
            note = "CAP REACHED during this request: it may have fallen back because a token cap refused a call"
        rows.append({
            "request_id": rid, "question_id": qid, "category": q.category, "answerable": "yes" if q.answerable else "no",
            "status": done.get("status"), "answer_mode": done.get("answer_mode") or "",
            "retrieval_setting": done.get("retrieval_setting") or "", "question": q.text, "gold": gold_summary(q),
            "answer": (evs.get("first_token") or {}).get("answer", ""), "detail_grade": "", "note": note,
        })
    return rows


def write_sheet(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(SHEET_COLUMNS), extrasaction="raise")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in SHEET_COLUMNS})


def read_sheet(path: Path) -> list:
    if not path.is_file():
        raise RelayError(f"{path} not found. Run grade-sheet first.")
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def derive_grades(sheet: Sequence[Mapping[str, str]], *, allow_partial: bool = False) -> dict:
    """request_id -> (detail_grade, answer_grade). Raises on any invalid or inconsistent entry."""
    out: dict = {}
    problems: list = []
    for r in sheet:
        rid = r["request_id"]
        detail = (r.get("detail_grade") or "").strip()
        allowed = allowed_details(r["answerable"] == "yes", r["status"], r["answer_mode"])
        if not allowed:
            if detail:
                problems.append(f"{rid}: not gradable (status={r['status']}, mode={r['answer_mode']}) but graded '{detail}'")
            continue
        if not detail:
            if not allow_partial:
                problems.append(f"{rid} ({r['question_id']}): no grade yet; allowed: {', '.join(allowed)}")
            continue
        if detail not in DETAIL_TO_CSV:
            problems.append(f"{rid}: unknown grade '{detail}'")
        elif detail not in allowed:
            problems.append(f"{rid} ({r['question_id']}): '{detail}' does not fit a "
                            f"{'answerable' if r['answerable'] == 'yes' else 'unanswerable'} question answered in "
                            f"mode '{r['answer_mode']}'; allowed: {', '.join(allowed)}")
        else:
            out[rid] = (detail, DETAIL_TO_CSV[detail])
    if problems:
        raise RelayError("grading sheet problems:\n  " + "\n  ".join(problems))
    return out


def apply_grades(results_path: Path, sheet: Sequence[Mapping[str, str]], *, allow_partial: bool = False) -> dict:
    """Write answer_grade into results.csv (a copy of the original is kept once as results.ungraded.csv)."""
    derived = derive_grades(sheet, allow_partial=allow_partial)
    rows = read_results_csv(results_path)
    known = {r["request_id"] for r in rows}
    stray = sorted(set(derived) - known)
    if stray:
        raise RelayError(f"grades for requests not in results.csv: {stray}")
    backup = results_path.with_name("results.ungraded.csv")
    if not backup.exists():
        backup.write_bytes(results_path.read_bytes())
    for r in rows:
        r["answer_grade"] = derived[r["request_id"]][1] if r["request_id"] in derived else ""
    write_results_csv(results_path, rows)
    by_mode: dict = {}
    mode_of = {r["request_id"]: r["answer_mode"] for r in sheet}
    for rid, (detail, _) in derived.items():
        key = f"{mode_of[rid]}:{detail}"
        by_mode[key] = by_mode.get(key, 0) + 1
    csv_counts: dict = {}
    for _, g in derived.values():
        csv_counts[g] = csv_counts.get(g, 0) + 1
    return {"graded_at": datetime.now(timezone.utc).isoformat(), "n_graded": len(derived),
            "n_ungraded_ok": sum(1 for r in sheet if allowed_details(r["answerable"] == "yes", r["status"], r["answer_mode"])) - len(derived),
            "answer_grade_counts": csv_counts, "by_mode_and_detail": by_mode,
            "mapping": dict(DETAIL_TO_CSV), "graded_by": "the author (manual)"}
