"""Offline retrieval evaluation against questions the user wrote.

Retrieval-only: no model calls, no network, no quota. It answers "does the
retriever fetch the passage that holds the answer, and how much context does
that cost?", not "is the final answer correct". Its results must be labelled
"retrieval-only" wherever they are reported.

A gold entry says where the answer lives:
    paper: <paper key>
    pages: [3, 4]                    # optional; PDF page indexes
    evidence: ["scaled dot product"] # optional; phrases that must all occur in the chunk
A chunk satisfies an entry if the paper matches, its page range overlaps
`pages` (when given), and it contains every `evidence` phrase (when given,
compared after normalising case and punctuation). An entry needs at least one
of `pages` / `evidence`.

Per question:  hit_any = some retrieved chunk satisfies some gold entry,
               hit_all = every gold entry is satisfied by some retrieved chunk.
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
from .textutil import normalize_for_match

PROVENANCE = "retrieval-only, offline, no model calls"


class QuestionsError(RelayError):
    """The questions file is missing or invalid."""


@dataclass(frozen=True)
class Gold:
    paper: str
    pages: tuple[int, ...] = ()
    evidence: tuple[str, ...] = ()


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
            evidence = tuple(str(x) for x in (g.get("evidence") or []))
            if not pages and not evidence:
                raise QuestionsError(f"{where} ({qid}): each gold entry needs 'pages' or 'evidence'")
            golds.append(Gold(paper=g["paper"], pages=pages, evidence=evidence))
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


def chunk_satisfies(chunk: Chunk, gold: Gold) -> bool:
    if chunk.paper_key != gold.paper:
        return False
    if gold.pages and not any(chunk.page_start <= p <= chunk.page_end for p in gold.pages):
        return False
    if gold.evidence:
        hay = f" {normalize_for_match(chunk.text)} "
        for phrase in gold.evidence:
            needle = normalize_for_match(phrase)
            if needle and f" {needle} " not in hay:
                return False
    return True


def evaluate(retrievers: Mapping[str, Retriever], questions: Sequence[Question]) -> dict[str, Any]:
    """Run every question through every retriever. Pure function of its inputs."""
    settings_out: dict[str, Any] = {}
    per_question: list[dict[str, Any]] = []
    for sid, retriever in retrievers.items():
        ans = [q for q in questions if q.answerable]
        unans = [q for q in questions if not q.answerable]
        hit_any = hit_all = false_abstain = correct_abstain = 0
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
                sat = [any(chunk_satisfies(c, g) for c in chunks) for g in q.gold]
                row["hit_any"], row["hit_all"] = any(sat), all(sat)
                hit_any += row["hit_any"]
                hit_all += row["hit_all"]
                false_abstain += res.abstain
            else:
                correct_abstain += res.abstain
            per_question.append(row)

        def pct(n: int, d: int) -> Optional[float]:
            return round(100.0 * n / d, 1) if d else None

        settings_out[sid] = {
            "n_answerable": len(ans), "n_unanswerable": len(unans),
            "hit_any_pct": pct(hit_any, len(ans)), "hit_all_pct": pct(hit_all, len(ans)),
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
        ("hit_all%", "hit_all_pct"), ("false_abstain%", "false_abstain_pct"),
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
