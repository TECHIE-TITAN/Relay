"""Shared data types and tiny JSONL helpers.

Plain dataclasses with explicit dict conversion, so the on-disk JSONL stays
human-readable and stable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional


class RelayError(Exception):
    """Base class for errors whose message is meant to be shown to the user."""


@dataclass(frozen=True)
class Block:
    """One text block from a PDF page (a paragraph, a heading, ...)."""

    text: str
    kind: str = "body"            # "body" or "heading"
    in_references: bool = False   # True inside the reference list

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "kind": self.kind, "in_references": self.in_references}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Block":
        return cls(
            text=d["text"],
            kind=d.get("kind", "body"),
            in_references=bool(d.get("in_references", False)),
        )


@dataclass(frozen=True)
class PageRecord:
    """Cleaned content of one PDF page. `page` is the 1-based PDF page index."""

    paper_key: str
    page: int
    blocks: tuple[Block, ...]
    dropped: int = 0   # header/footer/rotated blocks removed from this page

    def to_dict(self) -> dict[str, Any]:
        return {
            "paper_key": self.paper_key,
            "page": self.page,
            "blocks": [b.to_dict() for b in self.blocks],
            "dropped": self.dropped,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PageRecord":
        return cls(
            paper_key=d["paper_key"],
            page=int(d["page"]),
            blocks=tuple(Block.from_dict(b) for b in d.get("blocks", [])),
            dropped=int(d.get("dropped", 0)),
        )


@dataclass(frozen=True)
class Chunk:
    """A retrievable piece of one paper. Pages are PDF page indexes (1-based)."""

    chunk_id: str
    paper_key: str
    setting_id: str
    page_start: int
    page_end: int
    section: Optional[str]
    text: str
    approx_tokens: int   # characters / 4; NOT a real token count

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "paper_key": self.paper_key,
            "setting_id": self.setting_id,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "section": self.section,
            "text": self.text,
            "approx_tokens": self.approx_tokens,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Chunk":
        return cls(
            chunk_id=d["chunk_id"],
            paper_key=d["paper_key"],
            setting_id=d["setting_id"],
            page_start=int(d["page_start"]),
            page_end=int(d["page_end"]),
            section=d.get("section"),
            text=d["text"],
            approx_tokens=int(d["approx_tokens"]),
        )


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float
    rank: int   # 1-based


@dataclass(frozen=True)
class SearchResult:
    query: str
    query_tokens: tuple[str, ...]
    hits: tuple[Hit, ...]
    abstain: bool
    abstain_reason: Optional[str]   # None | "empty_query" | "no_match" | "low_coverage"
    coverage: float                 # share of distinct query terms found in the top hit
    elapsed_ms: float


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    """Write rows as JSON lines (UTF-8). Returns the number of rows written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            f.write("\n")
            n += 1
    return n


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                raise RelayError(f"{path}:{lineno}: invalid JSON ({e})") from e
