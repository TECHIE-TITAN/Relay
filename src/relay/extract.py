"""PDF to cleaned, page-addressed text.

Pipeline: PyMuPDF text blocks -> drop repeated headers/footers and rotated
margin text -> classify headings -> mark the reference list -> PageRecords.

Heuristics (all inspectable with `cli inspect`):
  * block order is the PDF's content-stream order, which for LaTeX output is
    normally reading order, column by column. Not verified for every paper.
  * headings: larger-than-body font, or bold and numbered/known ("3.1 ...",
    "Abstract", "References", "Appendix ...").
  * hyphenated line breaks are joined ("informa-" + "tion"). A genuine
    compound split across lines ("end-to-" + "end") is joined wrongly; this is
    a known, accepted source of retrieval error.
  * tables are NOT reconstructed; their cells come out as loose text.
Pages are PDF page indexes (1-based), not printed page labels.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:  # PyMuPDF >= 1.24.3 exposes `pymupdf`; older versions only `fitz`
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

from .schema import Block, PageRecord, RelayError, read_jsonl, write_jsonl

_STAMP_RE = re.compile(r"arXiv:(\d{4}\.\d{4,5})v(\d+)")
_STAMP_REVERSED_RE = re.compile(r"(\d+)v(\d{4,5}\.\d{4,5}):viXra")
_BOLD_FONT_HINTS = ("bold", "black", "heavy", "semibold", "medi")
_HEADING_NUM_RE = re.compile(r"^(\d+(\.\d+){0,3}|[A-Z](\.\d+){0,3})\.?\s+\S")
_KNOWN_HEADINGS = {
    "abstract", "introduction", "conclusion", "conclusions", "acknowledgments",
    "acknowledgements", "references", "bibliography", "appendix",
}
_REFERENCE_HEADINGS = {"references", "bibliography"}


@dataclass
class RawBlock:
    page: int
    text: str
    size: float          # largest font size in the block
    bold_frac: float     # share of characters in bold fonts
    n_lines: int
    y0: float
    y1: float
    page_height: float
    rotated: bool = False
    x0: float = 0.0      # left edge of the block


# ---------------------------------------------------------------- text cleaning

def clean_text(s: str) -> str:
    """NFKC (fixes ligatures such as 'fi'), drop soft hyphens and control chars."""
    s = unicodedata.normalize("NFKC", s).replace("­", "")
    s = "".join(ch for ch in s if ch in "\n\t " or unicodedata.category(ch)[0] != "C")
    return re.sub(r"\s+", " ", s).strip()


def dehyphenate_join(lines: list[str]) -> str:
    """Join lines of one block, repairing words hyphenated at a line break."""
    out = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if not out:
            out = line
        elif out.endswith("-") and len(out) >= 2 and out[-2].isalpha():
            if line[:1].islower():
                out = out[:-1] + line      # "informa-" + "tion" -> "information"
            else:
                out = out + line           # "well-" + "Known"   -> "well-Known"
        else:
            out = out + " " + line
    return out


def find_arxiv_stamp(text: str) -> Optional[tuple[str, int]]:
    """Find 'arXiv:<id>v<version>' in text. Also tries reversed text, because
    rotated margin text is sometimes extracted back to front."""
    m = _STAMP_RE.search(text)
    if m:
        return m.group(1), int(m.group(2))
    m = _STAMP_REVERSED_RE.search(text)   # the stamp was extracted back to front
    if m:
        return m.group(2)[::-1], int(m.group(1)[::-1])
    return None


def read_pdf_stamp(path: Path) -> tuple[Optional[tuple[str, int]], int]:
    """Return ((arxiv_id, version) or None, page_count), searching the first 3 pages."""
    with fitz.open(str(path)) as doc:
        n_pages = len(doc)
        for i in range(min(3, n_pages)):
            stamp = find_arxiv_stamp(doc[i].get_text())
            if stamp:
                return stamp, n_pages
    return None, n_pages


# ------------------------------------------------------------- block collection

def collect_blocks(doc: Any) -> list[RawBlock]:
    out: list[RawBlock] = []
    for pno in range(len(doc)):
        page = doc[pno]
        height = float(page.rect.height)
        for b in page.get_text("dict").get("blocks", []):
            if b.get("type") != 0:
                continue
            lines: list[str] = []
            sizes: list[float] = []
            chars = bold_chars = 0
            rotated = False
            for ln in b.get("lines", []):
                dx, _dy = ln.get("dir", (1.0, 0.0))
                if abs(dx) < 0.9:
                    rotated = True
                parts: list[str] = []
                for sp in ln.get("spans", []):
                    t = sp.get("text", "")
                    parts.append(t)
                    n = len(t.strip())
                    if not n:
                        continue
                    chars += n
                    sizes.append(float(sp.get("size", 0.0)))
                    font = str(sp.get("font", "")).lower()
                    if (int(sp.get("flags", 0)) & 16) or any(h in font for h in _BOLD_FONT_HINTS):
                        bold_chars += n
                line_text = "".join(parts).strip()
                if line_text:
                    lines.append(line_text)
            if not lines:
                continue
            text = clean_text(dehyphenate_join(lines))
            if not text:
                continue
            out.append(
                RawBlock(
                    page=pno + 1,
                    text=text,
                    size=max(sizes) if sizes else 0.0,
                    bold_frac=(bold_chars / chars) if chars else 0.0,
                    n_lines=len(lines),
                    x0=float(b["bbox"][0]),
                    y0=float(b["bbox"][1]),
                    y1=float(b["bbox"][3]),
                    page_height=height,
                    rotated=rotated,
                )
            )
    return out


def body_font_size(blocks: list[RawBlock]) -> float:
    """Most common font size, weighted by characters (the body text size)."""
    counts: Counter[float] = Counter()
    for b in blocks:
        if not b.rotated:
            counts[round(b.size, 1)] += len(b.text)
    return counts.most_common(1)[0][0] if counts else 10.0


def body_left_margin(blocks: list[RawBlock]) -> Optional[float]:
    """Most common left edge of multi-line paragraphs, weighted by characters.

    Real section headings start at this margin; labels inside figures usually
    do not. Returns None if there are no multi-line paragraphs.
    """
    counts: Counter[float] = Counter()
    for b in blocks:
        if not b.rotated and b.n_lines >= 2:
            counts[round(b.x0)] += len(b.text)
    return float(counts.most_common(1)[0][0]) if counts else None


def _margin_key(text: str) -> str:
    return re.sub(r"\d+", "#", text.lower()).strip()


def drop_margin_noise(blocks: list[RawBlock], n_pages: int) -> tuple[list[RawBlock], dict[int, int]]:
    """Remove rotated blocks, bare page numbers and headers/footers that repeat.

    A margin block (top or bottom 10% of the page) is dropped if its text, with
    digits masked, appears on at least max(3, 30% of pages) distinct pages.
    """
    def in_margin(b: RawBlock) -> bool:
        return b.page_height > 0 and (
            b.y1 <= 0.10 * b.page_height or b.y0 >= 0.90 * b.page_height
        )

    pages_by_key: dict[str, set[int]] = {}
    for b in blocks:
        if in_margin(b) and not b.rotated:
            pages_by_key.setdefault(_margin_key(b.text), set()).add(b.page)
    threshold = max(3, math.ceil(0.3 * n_pages))
    repeated = {k for k, pages in pages_by_key.items() if len(pages) >= threshold}

    kept: list[RawBlock] = []
    dropped: dict[int, int] = {}
    for b in blocks:
        drop = False
        if b.rotated:
            drop = True
        elif in_margin(b) and (
            _margin_key(b.text) in repeated or re.fullmatch(r"\d{1,4}", b.text.strip())
        ):
            drop = True
        if drop:
            dropped[b.page] = dropped.get(b.page, 0) + 1
        else:
            kept.append(b)
    return kept, dropped


# ----------------------------------------------------------- headings, sections

def _heading_key(text: str) -> str:
    return re.sub(r"^[\d.\s]+", "", text.lower()).strip(" .:")


def is_heading(b: RawBlock, body_size: float) -> bool:
    t = b.text
    if b.rotated or not t or len(t) > 120 or b.n_lines > 2 or len(t.split()) > 18:
        return False
    key = _heading_key(t)
    known = key in _KNOWN_HEADINGS or key.startswith("appendix")
    if t.endswith(".") and not known:
        return False
    larger = body_size > 0 and b.size >= body_size * 1.12
    bold = b.bold_frac >= 0.8
    numbered = bool(_HEADING_NUM_RE.match(t))
    if larger:
        return True
    if bold and (numbered or known):
        return True
    return known and len(t) < 30


def _strong_heading(b: RawBlock) -> bool:
    """Evidence for a heading beyond font size: numbered, a known heading word, or page 1 (title)."""
    key = _heading_key(b.text)
    known = key in _KNOWN_HEADINGS or key.startswith("appendix")
    return known or bool(_HEADING_NUM_RE.match(b.text)) or b.page == 1


def build_page_records(
    paper_key: str,
    kept: list[RawBlock],
    body_size: float,
    n_pages: int,
    dropped_by_page: dict[int, int],
    body_x0: Optional[float] = None,
) -> tuple[list[PageRecord], dict[str, Any]]:
    by_page: dict[int, list[RawBlock]] = {p: [] for p in range(1, n_pages + 1)}
    for b in kept:
        by_page.setdefault(b.page, []).append(b)

    in_refs = False
    refs_start: Optional[int] = None
    headings: list[tuple[int, str]] = []
    records: list[PageRecord] = []
    for p in range(1, n_pages + 1):
        page_blocks = by_page.get(p, [])
        cand = [is_heading(b, body_size) for b in page_blocks]
        # A large-font block with no other evidence is "weak". Figure labels come in
        # clusters, as stray letters, or away from the body's left margin, so a weak
        # candidate is rejected if it is next to another weak candidate, has fewer
        # than 4 letters, or starts more than 4 points from the left margin.
        weak = [c and not _strong_heading(b) for c, b in zip(cand, page_blocks)]
        for i, b in enumerate(page_blocks):
            if weak[i]:
                neighbour_weak = (i > 0 and weak[i - 1]) or (i + 1 < len(weak) and weak[i + 1])
                off_margin = body_x0 is not None and abs(b.x0 - body_x0) > 4.0
                if neighbour_weak or off_margin or sum(ch.isalpha() for ch in b.text) < 4:
                    cand[i] = False
        blocks: list[Block] = []
        for b, heading in zip(page_blocks, cand):
            if heading:
                headings.append((p, b.text))
                key = _heading_key(b.text)
                if key in _REFERENCE_HEADINGS:
                    in_refs = True
                    if refs_start is None:
                        refs_start = p
                elif in_refs:
                    in_refs = False   # any other heading ends the reference list
            blocks.append(
                Block(text=b.text, kind="heading" if heading else "body", in_references=in_refs)
            )
        records.append(
            PageRecord(paper_key=paper_key, page=p, blocks=tuple(blocks), dropped=dropped_by_page.get(p, 0))
        )
    return records, {"headings": headings, "references_start_page": refs_start}


# ------------------------------------------------------------------ entry points

def extract_pdf(path: Path, paper_key: str) -> tuple[list[PageRecord], dict[str, Any]]:
    """Extract one PDF. Returns the page records and a small report."""
    with fitz.open(str(path)) as doc:
        n_pages = len(doc)
        raw = collect_blocks(doc)
    kept, dropped = drop_margin_noise(raw, n_pages)
    body = body_font_size(kept)
    body_x0 = body_left_margin(kept)
    records, meta = build_page_records(paper_key, kept, body, n_pages, dropped, body_x0)
    report = {
        "paper_key": paper_key,
        "pages": n_pages,
        "blocks_extracted": len(raw),
        "blocks_kept": len(kept),
        "blocks_dropped": sum(dropped.values()),
        "body_font_size": body,
        "body_left_margin": body_x0,
        "headings_found": len(meta["headings"]),
        "headings": [{"page": p, "text": t} for p, t in meta["headings"]],
        "references_start_page": meta["references_start_page"],
        "reference_pages": [r.page for r in records if any(b.in_references for b in r.blocks)],
        "dropped_by_page": {str(p): n for p, n in sorted(dropped.items())},
        "pages_without_text": [r.page for r in records if not r.blocks],
    }
    return records, report


def write_pages(path: Path, records: list[PageRecord]) -> int:
    return write_jsonl(path, (r.to_dict() for r in records))


def read_pages(path: Path) -> list[PageRecord]:
    if not path.is_file():
        raise RelayError(f"{path} not found. Run `python -m relay.cli extract` first.")
    return [PageRecord.from_dict(d) for d in read_jsonl(path)]
