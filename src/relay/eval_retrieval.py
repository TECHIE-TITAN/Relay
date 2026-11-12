"""Offline retrieval evaluation against questions the user wrote.

Retrieval-only: no model calls, no network, no quota. It answers "does the
retriever fetch the passage that holds the answer, and how much context does
that cost?", not "is the final answer correct". Its results must be labelled
"retrieval-only" wherever they are reported.

A gold entry says where the answer lives:
    paper: <paper key>
    pages: [3, 4]                    # optional; PDF page indexes
    evidence: ["scaled dot product"] # optional; phrases that must all occur in the chunk
`pages` are 1-based PHYSICAL PDF page indexes (position in the file);
`printed_pages` are the page numbers typeset on the pages. They can differ and
are matched separately; if both are given, both must match. An entry needs at
least one of `pages` / `printed_pages` / `evidence`. Optional `group`: entries
sharing a group are ALTERNATIVE locations of the same fact (any one will do);
entries in different groups are all required.

An entry is satisfied by the RETRIEVED SET (the chunks the generator would
see): among retrieved chunks of the right paper whose page range overlaps
`pages` (when given), every `evidence` phrase must occur somewhere (case and
punctuation ignored). The older, stricter test, all phrases inside ONE chunk,
is still computed and reported as `strict`; it penalises small chunks that
split adjacent sentences.

Per question:  hit_any = some entry satisfied,
               hit_all = every group has a satisfied entry,
               strict  = hit_all using the single-chunk test,
               evidence_recall = mean over groups of the best fraction of
                                 evidence phrases found.
Unanswerable questions have no gold; the correct behaviour is to abstain.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import yaml

from .retrieve import Retriever
from .schema import Chunk, RelayError
from .textutil import normalize_for_match, phrase_in

PROVENANCE = "retrieval-only, offline, no model calls"


class QuestionsError(RelayError):
    """The questions file is missing or invalid."""


@dataclass(frozen=True)
class Gold:
    paper: str
    pages: tuple[int, ...] = ()
    evidence: tuple[str, ...] = ()
    group: Optional[str] = None   # entries sharing a group are alternatives
    printed_pages: tuple[int, ...] = ()   # numbers typeset on the page (not physical indexes)


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    answerable: bool
    category: str
    gold: tuple[Gold, ...] = field(default_factory=tuple)


def load_questions(path: Path, valid_papers: Sequence[str]) -> list[Question]:
    if not path.is_file():
        raise QuestionsError(
            f"{path} not found. Copy data/questions.template.yaml to data/questions.yaml "
            f"and write your own questions (the brief requires questions you wrote yourself)."
        )
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise QuestionsError(f"{path}: invalid YAML ({e})") from e
    items = (doc or {}).get("questions") if isinstance(doc, dict) else None
    if not isinstance(items, list) or not items:
        raise QuestionsError(f"{path}: expected a non-empty 'questions' list")

    out: list[Question] = []
    seen: set[str] = set()
    for n, q in enumerate(items, 1):
        where = f"{path.name} question #{n}"
        if not isinstance(q, dict):
            raise QuestionsError(f"{where}: expected a mapping")
        qid = str(q.get("id", "")).strip()
        text = str(q.get("text", "")).strip()
        if not qid or not text:
            raise QuestionsError(f"{where}: 'id' and 'text' are required")
        if qid in seen:
            raise QuestionsError(f"{where}: duplicate id '{qid}'")
        seen.add(qid)
        if not isinstance(q.get("answerable"), bool):
            raise QuestionsError(f"{where} ({qid}): 'answerable' must be true or false")
        golds: list[Gold] = []
        for g in q.get("gold") or []:
            if not isinstance(g, dict) or g.get("paper") not in valid_papers:
                raise QuestionsError(
                    f"{where} ({qid}): gold.paper must be one of {list(valid_papers)}"
                )
            pages = tuple(int(x) for x in (g.get("pages") or []))
            printed = tuple(int(x) for x in (g.get("printed_pages") or []))
            evidence = tuple(str(x) for x in (g.get("evidence") or []))
            if not pages and not evidence and not printed:
                raise QuestionsError(
                    f"{where} ({qid}): each gold entry needs 'pages' or 'evidence' (or 'printed_pages')"
                )
            group = str(g["group"]) if g.get("group") is not None else None
            golds.append(
                Gold(paper=g["paper"], pages=pages, evidence=evidence, group=group, printed_pages=printed)
            )
        if q["answerable"] and not golds:
            raise QuestionsError(f"{where} ({qid}): an answerable question needs at least one gold entry")
        if not q["answerable"] and golds:
            raise QuestionsError(f"{where} ({qid}): an unanswerable question must have no gold entries")
        out.append(
            Question(
                id=qid, text=text, answerable=q["answerable"],
                category=str(q.get("category", "")), gold=tuple(golds),
            )
        )
    return out


def _printed_range(chunk: Chunk) -> Optional[tuple[int, int]]:
    try:
        return int(chunk.printed_start), int(chunk.printed_end)   # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _page_ok(chunk: Chunk, gold: Gold) -> bool:
    """Physical pages and printed pages are checked separately; both must hold if both are given."""
    if gold.pages and not any(chunk.page_start <= p <= chunk.page_end for p in gold.pages):
        return False
    if gold.printed_pages:
        rng = _printed_range(chunk)
        if rng is None or not any(rng[0] <= p <= rng[1] for p in gold.printed_pages):
            return False
    return True


def chunk_satisfies(chunk: Chunk, gold: Gold) -> bool:
    if chunk.paper_key != gold.paper:
        return False
    if not _page_ok(chunk, gold):
        return False
    if gold.evidence:
        hay = normalize_for_match(chunk.text)
        for phrase in gold.evidence:
            if not phrase_in(hay, phrase):
                return False
    return True


def _relevant(chunks: Sequence[Chunk], gold: Gold) -> list[Chunk]:
    """Retrieved chunks of the gold paper whose page range overlaps gold.pages."""
    return [c for c in chunks if c.paper_key == gold.paper and _page_ok(c, gold)]


def evidence_fraction(chunks: Sequence[Chunk], gold: Gold) -> float:
    """Share of the entry's evidence phrases found in the retrieved set.

    0.0 if no retrieved chunk is in the right paper and pages. An entry with no
    evidence phrases (pages only) scores 1.0 once such a chunk is retrieved.
    """
    rel = _relevant(chunks, gold)
    if not rel:
        return 0.0
    if not gold.evidence:
        return 1.0
    hays = [normalize_for_match(c.text) for c in rel]
    found = sum(1 for phrase in gold.evidence if any(phrase_in(h, phrase) for h in hays))
    return found / len(gold.evidence)


def entry_satisfied(chunks: Sequence[Chunk], gold: Gold) -> bool:
    return evidence_fraction(chunks, gold) >= 1.0


def _groups(golds: Sequence[Gold]) -> list[list[Gold]]:
    """Alternatives (same `group`) are bundled; every other entry is its own group."""
    out: dict[str, list[Gold]] = {}
    for i, g in enumerate(golds):
        out.setdefault(g.group if g.group else f"#{i}", []).append(g)
    return list(out.values())


def evaluate(retrievers: Mapping[str, Retriever], questions: Sequence[Question]) -> dict[str, Any]:
    """Run every question through every retriever. Pure function of its inputs."""
    settings_out: dict[str, Any] = {}
    per_question: list[dict[str, Any]] = []
    for sid, retriever in retrievers.items():
        ans = [q for q in questions if q.answerable]
        unans = [q for q in questions if not q.answerable]
        hit_any = hit_all = strict_all = false_abstain = correct_abstain = 0
        recalls: list[float] = []
        context_tokens: list[int] = []
        latencies: list[float] = []
        for q in questions:
            res = retriever.search(q.text)
            chunks = [h.chunk for h in res.hits]
            tokens = sum(c.approx_tokens for c in chunks)
            context_tokens.append(tokens)
            latencies.append(res.elapsed_ms)
            row: dict[str, Any] = {
                "setting": sid, "id": q.id, "answerable": q.answerable,
                "abstain": res.abstain, "abstain_reason": res.abstain_reason,
                "coverage": round(res.coverage, 4), "context_tokens_approx": tokens,
                "chunk_ids": [c.chunk_id for c in chunks],
            }
            if q.answerable:
                groups = _groups(q.gold)
                union = [[entry_satisfied(chunks, g) for g in grp] for grp in groups]
                single = [[any(chunk_satisfies(c, g) for c in chunks) for g in grp] for grp in groups]
                recall = [max(evidence_fraction(chunks, g) for g in grp) for grp in groups]
                row["hit_any"] = any(any(u) for u in union)
                row["hit_all"] = all(any(u) for u in union)
                row["hit_all_single_chunk"] = all(any(s) for s in single)
                row["evidence_recall"] = round(statistics.fmean(recall), 4)
                hit_any += row["hit_any"]
                hit_all += row["hit_all"]
                strict_all += row["hit_all_single_chunk"]
                recalls.append(row["evidence_recall"])
                false_abstain += res.abstain
            else:
                correct_abstain += res.abstain
            per_question.append(row)

        def pct(n: int, d: int) -> Optional[float]:
            return round(100.0 * n / d, 1) if d else None

        settings_out[sid] = {
            "n_answerable": len(ans), "n_unanswerable": len(unans),
            "hit_any_pct": pct(hit_any, len(ans)), "hit_all_pct": pct(hit_all, len(ans)),
            "strict_all_pct": pct(strict_all, len(ans)),
            "evidence_recall_pct": round(100.0 * statistics.fmean(recalls), 1) if recalls else None,
            "false_abstain_pct": pct(false_abstain, len(ans)),
            "correct_abstain_pct": pct(correct_abstain, len(unans)),
            "mean_context_tokens_approx": round(statistics.fmean(context_tokens), 1) if context_tokens else None,
            "median_search_ms": round(statistics.median(latencies), 3) if latencies else None,
            "max_search_ms": round(max(latencies), 3) if latencies else None,
        }
    return {"settings": settings_out, "per_question": per_question}


def format_table(result: Mapping[str, Any]) -> str:
    cols = [
        ("setting", None), ("answerable", "n_answerable"), ("hit_any%", "hit_any_pct"),
        ("hit_all%", "hit_all_pct"), ("strict%", "strict_all_pct"),
        ("ev_recall%", "evidence_recall_pct"), ("false_abstain%", "false_abstain_pct"),
        ("unanswerable", "n_unanswerable"), ("correct_abstain%", "correct_abstain_pct"),
        ("ctx_tokens~", "mean_context_tokens_approx"), ("median_ms", "median_search_ms"),
    ]
    rows = [[c[0] for c in cols]]
    for sid, m in result["settings"].items():
        rows.append([sid] + [("n/a" if m[k] is None else str(m[k])) for _, k in cols[1:]])
    widths = [max(len(r[i]) for r in rows) for i in range(len(cols))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)) for r in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def save_result(directory: Path, questions_file: Path, n_questions: int, result: Mapping[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc)
    path = directory / f"retrieval_eval_{stamp.strftime('%Y%m%dT%H%M%SZ')}.json"
    payload = {
        "kind": "retrieval_eval",
        "provenance": PROVENANCE,
        "generated_at": stamp.isoformat(timespec="seconds"),
        "questions_file": str(questions_file),
        "n_questions": n_questions,
        **result,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
