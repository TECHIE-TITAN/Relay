"""Split a paper's pages into retrievable chunks.

Algorithm (deterministic, word-window based):
  1. Flatten the paper into words, each remembering its PDF page and section.
     Blocks inside the reference list are skipped (excluded from the index).
  2. If the setting is section-aware, cut the word stream into segments at
     every heading. A segment whose size is below MIN_SEGMENT_TOKENS (for
     example a bare "3 Model Architecture" heading followed at once by a
     sub-heading) is merged into the next segment.
  3. Slide a window over each segment. A window ends at the first word where
     its approximate token cost reaches `chunk_tokens`. The next window starts
     `overlap_tokens` earlier (at least one word of progress). If fewer than
     TAIL_FRACTION * chunk_tokens remain after a window, the window absorbs
     them, so no tiny tail chunk is produced. A chunk is therefore between
     chunk_tokens and about 1.25 * chunk_tokens, except for segments shorter
     than chunk_tokens.
  4. chunk.section is the section of the chunk's middle word; page_start and
     page_end are the pages of its first and last word.

Costs use `word_cost` (characters / 4), an approximation. Tradeoff: paragraph
and sentence boundaries are not respected inside a section.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, Optional, Sequence

from .config import RetrievalSetting
from .schema import Chunk, PageRecord, RelayError, read_jsonl, write_jsonl
from .textutil import approx_tokens, word_cost

MIN_SEGMENT_TOKENS = 64
TAIL_FRACTION = 0.25


class _Word(NamedTuple):
    text: str
    page: int
    section: Optional[str]


def _segment_cost(seg: Sequence[_Word]) -> float:
    return sum(word_cost(w.text) for w in seg)


def _merge_tiny(segments: list[list[_Word]]) -> list[list[_Word]]:
    out: list[list[_Word]] = []
    carry: list[_Word] = []
    for seg in segments:
        seg = carry + seg
        carry = []
        if _segment_cost(seg) < MIN_SEGMENT_TOKENS:
            carry = seg
            continue
        out.append(seg)
    if carry:
        if out:
            out[-1] = out[-1] + carry
        else:
            out.append(carry)
    return out


def _segments(pages: Sequence[PageRecord], section_aware: bool) -> list[list[_Word]]:
    segments: list[list[_Word]] = [[]]
    section: Optional[str] = None
    for rec in pages:
        for blk in rec.blocks:
            if blk.in_references:
                continue
            if blk.kind == "heading":
                section = blk.text
                if section_aware and segments[-1]:
                    segments.append([])
            for w in blk.text.split():
                segments[-1].append(_Word(w, rec.page, section))
    segments = [s for s in segments if s]
    return _merge_tiny(segments) if section_aware else segments


def _windows(words: Sequence[_Word], size: float, overlap: float) -> list[tuple[int, int]]:
    n = len(words)
    costs = [word_cost(w.text) for w in words]
    out: list[tuple[int, int]] = []
    s = 0
    while s < n:
        e = s
        acc = 0.0
        while e < n and acc < size:
            acc += costs[e]
            e += 1
        if e < n and sum(costs[e:]) < TAIL_FRACTION * size:
            e = n
        out.append((s, e))
        if e >= n:
            break
        ns = e
        back = 0.0
        while ns > s + 1 and back < overlap:
            ns -= 1
            back += costs[ns]
        s = ns
    return out


def build_chunks(pages: Sequence[PageRecord], setting: RetrievalSetting) -> list[Chunk]:
    """Chunk the pages of exactly one paper."""
    if not pages:
        return []
    keys = {p.paper_key for p in pages}
    if len(keys) != 1:
        raise ValueError(f"build_chunks expects pages of one paper, got {sorted(keys)}")
    ordered = sorted(pages, key=lambda p: p.page)
    paper_key = ordered[0].paper_key

    chunks: list[Chunk] = []
    idx = 0
    for seg in _segments(ordered, setting.section_aware):
        for s, e in _windows(seg, setting.chunk_tokens, setting.overlap_tokens):
            words = seg[s:e]
            text = " ".join(w.text for w in words)
            chunks.append(
                Chunk(
                    chunk_id=f"{paper_key}:{setting.id}:{idx:04d}",
                    paper_key=paper_key,
                    setting_id=setting.id,
                    page_start=words[0].page,
                    page_end=words[-1].page,
                    section=words[len(words) // 2].section,
                    text=text,
                    approx_tokens=approx_tokens(text),
                )
            )
            idx += 1
    return chunks


def build_all(pages: Sequence[PageRecord], paper_keys: Sequence[str], setting: RetrievalSetting) -> list[Chunk]:
    """Chunk every paper (in the given key order) for one setting."""
    chunks: list[Chunk] = []
    for key in paper_keys:
        chunks.extend(build_chunks([p for p in pages if p.paper_key == key], setting))
    return chunks


def write_chunks(path: Path, chunks: Sequence[Chunk]) -> int:
    return write_jsonl(path, (c.to_dict() for c in chunks))


def read_chunks(path: Path) -> list[Chunk]:
    if not path.is_file():
        raise RelayError(f"{path} not found. Run `python -m relay.cli chunk` first.")
    return [Chunk.from_dict(d) for d in read_jsonl(path)]
