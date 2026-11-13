"""Prompt construction and citation rendering.

Sources are labelled [S1], [S2], ... in rank order. The model cites those labels; V0
verification checks them against the sources actually sent; after verification the
labels are rewritten into human-readable citations (paper, physical PDF page, section).
"""
from __future__ import annotations

import re
from typing import Mapping, Optional, Sequence

from .schema import Chunk, Hit

REFUSAL_TEXT = "The papers do not say."
NO_EVIDENCE_TEXT = "The three papers do not contain enough information to answer this question."

SYSTEM_PROMPT = (
    "You answer questions about three research papers using ONLY the numbered sources provided.\n"
    "Rules:\n"
    "1. End every factual sentence with its source label in square brackets, before the final period, "
    "for example: The encoder has six layers [S1].\n"
    "2. Use only facts, numbers and names that appear in the source you cite.\n"
    f"3. If the sources do not contain the answer, reply with exactly: {REFUSAL_TEXT}\n"
    "4. Be concise: at most three sentences. Do not use quotation marks unless you copy words exactly."
)


def source_map(hits: Sequence[Hit]) -> dict[str, Chunk]:
    return {f"S{i}": h.chunk for i, h in enumerate(hits, 1)}


def _span(a: object, b: object) -> str:
    return str(a) if a == b else f"{a}-{b}"


def cite_label(c: Chunk) -> str:
    """'attention, pdf p.6 (printed 5), §3.5 Positional Encoding'. Physical index first, printed number separate."""
    s = f"{c.paper_key}, pdf p.{_span(c.page_start, c.page_end)}"
    if c.printed_start is not None and c.printed_end is not None:
        s += f" (printed {_span(c.printed_start, c.printed_end)})"
    if c.section:
        s += f", §{c.section}"
    return s


def build_context(hits: Sequence[Hit]) -> str:
    blocks = []
    for i, h in enumerate(hits, 1):
        c = h.chunk
        blocks.append(f"[S{i}] {c.paper_key} | pdf p.{_span(c.page_start, c.page_end)} | {c.section or 'no section'}\n{c.text}")
    return "\n\n".join(blocks)


def build_messages(question: str, hits: Sequence[Hit], *, strict_reasons: Optional[Sequence[str]] = None) -> list:
    system = SYSTEM_PROMPT
    if strict_reasons:
        system += (
            "\nYour previous answer failed an automatic check (" + ", ".join(strict_reasons) + "). "
            "Every sentence must carry a citation; add no number, name or claim that is not in the cited "
            f"source; if you are not sure, answer exactly: {REFUSAL_TEXT}"
        )
    user = f"Sources:\n{build_context(hits)}\n\nQuestion: {question}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


_CITE_RE = re.compile(r"\[(S\d+(?:\s*,\s*S\d+)*)\]")


def render_citations(answer: str, sources: Mapping[str, Chunk]) -> str:
    """Replace [S1] / [S1, S2] with readable citations. Unknown labels are left as they are."""

    def sub(m: "re.Match[str]") -> str:
        ids = [x.strip() for x in m.group(1).split(",")]
        parts = [cite_label(sources[i]) if i in sources else i for i in ids]
        return "[" + "; ".join(parts) + "]"

    return _CITE_RE.sub(sub, answer)
