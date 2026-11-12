"""Check gold labels against the extracted text. Offline: no model calls.

For every evidence phrase of every gold entry, find the pages where the phrase
actually occurs and compare with the pages the label claims. Physical PDF page
indexes and printed page numbers are reported separately.

Statuses
  OK          the phrase occurs on a page the entry allows (or the entry has no page constraint)
  WRONG PAGE  the phrase occurs, but not on any page the entry lists
  NOT FOUND   the phrase occurs on no single page. It may span a page break, or
              differ from the extracted text (ligatures, split accents, math).

A phrase is searched page by page, after normalising case and punctuation.
Entries with only `pages` (no evidence phrase) cannot be checked.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from .eval_retrieval import Question
from .schema import PageRecord
from .textutil import normalize_for_match, phrase_in


def _page_text(rec: PageRecord) -> str:
    return normalize_for_match(" ".join(b.text for b in rec.blocks))


def _printed_int(rec: PageRecord) -> Optional[int]:
    s = rec.printed_page
    return int(s) if s and s.isdigit() else None


def check_gold(pages: Sequence[PageRecord], questions: Sequence[Question]) -> list[dict[str, Any]]:
    by_paper: dict[str, list[tuple[PageRecord, str]]] = {}
    for rec in pages:
        by_paper.setdefault(rec.paper_key, []).append((rec, _page_text(rec)))

    rows: list[dict[str, Any]] = []
    for q in questions:
        for g in q.gold:
            for phrase in g.evidence:
                found = [rec for rec, hay in by_paper.get(g.paper, []) if phrase_in(hay, phrase)]
                if not found:
                    status = "NOT FOUND"
                else:
                    ok = True
                    if g.pages:
                        ok = ok and any(r.page in g.pages for r in found)
                    if g.printed_pages:
                        ok = ok and any(_printed_int(r) in g.printed_pages for r in found)
                    status = "OK" if ok else "WRONG PAGE"
                rows.append({
                    "question": q.id,
                    "paper": g.paper,
                    "group": g.group,
                    "phrase": phrase,
                    "gold_pdf_pages": list(g.pages),
                    "gold_printed_pages": list(g.printed_pages),
                    "found_pdf_pages": [r.page for r in found],
                    "found_printed_pages": [r.printed_page for r in found],
                    "status": status,
                })
    return rows


def format_gold_check(rows: Sequence[dict[str, Any]]) -> str:
    lines: list[str] = []
    for r in rows:
        found = ", ".join(
            f"pdf p.{p}" + (f" (printed {pp})" if pp else "")
            for p, pp in zip(r["found_pdf_pages"], r["found_printed_pages"])
        ) or "on no single page"
        gold: list[str] = []
        if r["gold_pdf_pages"]:
            gold.append("pdf " + ",".join(map(str, r["gold_pdf_pages"])))
        if r["gold_printed_pages"]:
            gold.append("printed " + ",".join(map(str, r["gold_printed_pages"])))
        group = f" [group {r['group']}]" if r["group"] else ""
        lines.append(
            f"{r['status']:<10} {r['question']:<5} {r['paper']:<17}{group} "
            f"gold: {'; '.join(gold) or 'no page'} | found: {found}"
        )
        lines.append(f"           '{r['phrase'][:80]}'")
    return "\n".join(lines)
