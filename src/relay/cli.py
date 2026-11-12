"""Command line entry point:  python -m relay.cli <command>

Commands
  config          show the resolved configuration (the API key is never shown)
  ingest          download the three pinned PDFs from arXiv and verify versions (network: arxiv.org only)
  extract         PDFs -> cleaned pages (data/processed/pages.jsonl) + report
  chunk           pages -> chunks for each retrieval setting
  search          query a setting and print the hits
  inspect         show extracted blocks of one page (to see headings, noise, tables)
  eval-retrieval  score retrieval offline against your questions file
  check-gold      verify gold pages and evidence phrases against the extracted text

No command in this module calls the model provider.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import textwrap
from typing import Optional, Sequence

from .chunk import build_all, read_chunks, write_chunks
from .config import Config, load_config, get_api_key, mask_secret
from .eval_retrieval import (
    PROVENANCE, evaluate, format_table, load_questions, save_result,
)
from .extract import extract_pdf, read_pages, write_pages
from .gold_check import check_gold, format_gold_check
from .ingest import ingest
from .retrieve import Retriever
from .schema import RelayError


def _preview(text: str, width: int = 260) -> str:
    return textwrap.shorten(text, width=width, placeholder=" ...")


def _span(a: object, b: object) -> str:
    return str(a) if a == b else f"{a}-{b}"


def _page_label(c) -> str:
    """'pdf p.6-7 (printed 5-6)'. Physical PDF index first, printed number separately."""
    s = f"pdf p.{_span(c.page_start, c.page_end)}"
    if c.printed_start is not None and c.printed_end is not None:
        s += f" (printed {_span(c.printed_start, c.printed_end)})"
    return s


def _ranges(pages: Sequence[int]) -> str:
    """[1, 2, 3, 7, 9, 10] -> '1-3,7,9-10'"""
    out: list[str] = []
    start = prev = None
    for p in list(pages) + [None]:
        if start is not None and (p is None or p != prev + 1):
            out.append(str(start) if start == prev else f"{start}-{prev}")
            start = None
        if p is not None and start is None:
            start = p
        prev = p
    return ",".join(out) or "none"


def cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config()
    info = cfg.describe()
    info["api_key"] = mask_secret(get_api_key(cfg.root))
    info["paths_exist"] = {
        "manifest": cfg.paths.manifest.is_file(),
        "pages": cfg.paths.pages_file.is_file(),
        "questions": cfg.paths.questions.is_file(),
        **{f"chunks_{s}": cfg.paths.chunks_file(s).is_file() for s in cfg.settings},
    }
    print(json.dumps(info, indent=2))
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    cfg = load_config()
    result = ingest(cfg, force=args.force, allow_missing_stamp=args.allow_missing_stamp)
    for e in result.entries:
        print(
            f"{e['key']:<18} {result.statuses[e['key']]:<10} pages={e['pages']:<3} "
            f"stamp={e['stamp_found']} verified={e['stamp_verified']} sha256={e['sha256'][:12]}..."
        )
    print(f"manifest: {cfg.paths.manifest}")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    cfg = load_config()
    all_records = []
    reports = []
    for paper in cfg.papers:
        pdf = cfg.paths.papers_dir / paper.filename
        if not pdf.is_file():
            raise RelayError(f"{pdf} not found. Run `python -m relay.cli ingest` first.")
        records, report = extract_pdf(pdf, paper.key)
        all_records.extend(records)
        reports.append(report)
        print(
            f"{paper.key:<18} pages={report['pages']:<3} blocks kept={report['blocks_kept']:<5} "
            f"dropped={report['blocks_dropped']:<4} headings={report['headings_found']:<3} "
            f"references from page {report['references_start_page']}, "
            f"excluded pages={_ranges(report['reference_pages'])} "
            f"empty pages={report['pages_without_text']}"
        )
    n = write_pages(cfg.paths.pages_file, all_records)
    cfg.paths.extract_report.write_text(json.dumps(reports, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {n} pages to {cfg.paths.pages_file}")
    print(f"report: {cfg.paths.extract_report}")
    return 0


def cmd_chunk(args: argparse.Namespace) -> int:
    cfg = load_config()
    pages = read_pages(cfg.paths.pages_file)
    keys = [p.key for p in cfg.papers]
    for sid in args.setting or list(cfg.settings):
        setting = cfg.setting(sid)
        chunks = build_all(pages, keys, setting)
        write_chunks(cfg.paths.chunks_file(sid), chunks)
        sizes = [c.approx_tokens for c in chunks]
        per_paper = {k: sum(1 for c in chunks if c.paper_key == k) for k in keys}
        print(
            f"{sid}: {len(chunks)} chunks  tokens~ min/median/max = "
            f"{min(sizes)}/{int(statistics.median(sizes))}/{max(sizes)}  per paper {per_paper}"
        )
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    cfg = load_config()
    retriever = Retriever.from_config(cfg, args.setting)
    res = retriever.search(args.query, paper_keys=args.paper, k=args.k)
    print(f"setting: {retriever.setting.id}   query tokens: {list(res.query_tokens)}")
    print(
        f"coverage: {res.coverage:.2f}   abstain: {'YES (' + str(res.abstain_reason) + ')' if res.abstain else 'no'}"
        f"   {res.elapsed_ms:.1f} ms"
    )
    for h in res.hits:
        c = h.chunk
        print(f"\n#{h.rank}  score {h.score:.3f}  {c.chunk_id}  {c.paper_key} {_page_label(c)}  section: {c.section}")
        print("    " + _preview(c.text, 300))
    if not res.hits:
        print("\n(no hits)")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.paper(args.paper)   # validates the key
    pages = [p for p in read_pages(cfg.paths.pages_file) if p.paper_key == args.paper and p.page == args.page]
    if not pages:
        raise RelayError(f"no page {args.page} for '{args.paper}' in {cfg.paths.pages_file}")
    rec = pages[0]
    printed = f"printed {rec.printed_page}" if rec.printed_page else "no printed number found"
    print(
        f"{rec.paper_key} pdf page {rec.page} ({printed}): {len(rec.blocks)} blocks, "
        f"{rec.dropped} dropped as header/footer/rotated"
    )
    for i, b in enumerate(rec.blocks):
        tag = "H" if b.kind == "heading" else " "
        ref = "R" if b.in_references else " "
        text = b.text if args.full else _preview(b.text, 220)
        print(f"[{i:02d}{tag}{ref}] {text}")
    if args.setting:
        chunks = [
            c for c in read_chunks(cfg.paths.chunks_file(args.setting))
            if c.paper_key == args.paper and c.page_start <= args.page <= c.page_end
        ]
        print(f"\nchunks of {args.setting} touching this page:")
        for c in chunks:
            print(f"  {c.chunk_id}  {_page_label(c)}  ~{c.approx_tokens} tokens  section: {c.section}")
    return 0


def cmd_check_gold(args: argparse.Namespace) -> int:
    cfg = load_config()
    questions = load_questions(args.questions or cfg.paths.questions, [p.key for p in cfg.papers])
    rows = check_gold(read_pages(cfg.paths.pages_file), questions)
    print(format_gold_check(rows))
    bad = [r for r in rows if r["status"] != "OK"]
    print(f"\n{len(rows)} evidence phrases checked, {len(bad)} need attention")
    return 1 if bad else 0


def cmd_eval(args: argparse.Namespace) -> int:
    cfg = load_config()
    questions = load_questions(
        args.questions or cfg.paths.questions, [p.key for p in cfg.papers]
    )
    setting_ids = args.settings or list(cfg.settings)
    retrievers = {}
    for sid in setting_ids:
        if not cfg.paths.chunks_file(sid).is_file():
            print(f"skipping {sid}: no chunk file (run `chunk`)", file=sys.stderr)
            continue
        retrievers[sid] = Retriever.from_config(cfg, sid)
    if not retrievers:
        raise RelayError("no chunk files found. Run `python -m relay.cli chunk` first.")
    result = evaluate(retrievers, questions)
    print(f"[{PROVENANCE}]  questions: {len(questions)}\n")
    print(format_table(result))
    if not args.no_save:
        path = save_result(
            cfg.paths.retrieval_eval_dir, args.questions or cfg.paths.questions, len(questions), result
        )
        print(f"\nsaved: {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="relay", description="The Relay: Phase 0/1 tools")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("config", help="show resolved configuration").set_defaults(func=cmd_config)

    s = sub.add_parser("ingest", help="download and verify the pinned PDFs (arxiv.org)")
    s.add_argument("--force", action="store_true", help="download again even if cached")
    s.add_argument("--allow-missing-stamp", action="store_true",
                   help="accept a PDF with no readable arXiv version stamp (recorded as unverified)")
    s.set_defaults(func=cmd_ingest)

    sub.add_parser("extract", help="PDFs to cleaned pages").set_defaults(func=cmd_extract)

    s = sub.add_parser("chunk", help="pages to chunks")
    s.add_argument("--setting", action="append", help="only this setting (repeatable)")
    s.set_defaults(func=cmd_chunk)

    s = sub.add_parser("search", help="query the chunks")
    s.add_argument("query")
    s.add_argument("--setting", help="retrieval setting id (default from config)")
    s.add_argument("--paper", action="append", help="restrict to a paper key (repeatable)")
    s.add_argument("--k", type=int, help="override k")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("inspect", help="show extracted blocks of one page")
    s.add_argument("--paper", required=True)
    s.add_argument("--page", type=int, required=True, help="PDF page index, 1-based")
    s.add_argument("--setting", help="also list the chunks of this setting touching the page")
    s.add_argument("--full", action="store_true", help="do not truncate block text")
    s.set_defaults(func=cmd_inspect)

    s = sub.add_parser("eval-retrieval", help="offline retrieval scoring on your questions")
    s.add_argument("--questions", type=_path_arg, help="questions file (default from config)")
    s.add_argument("--settings", nargs="+", help="settings to score (default: all with chunk files)")
    s.add_argument("--no-save", action="store_true", help="do not write results/retrieval_eval/*.json")
    s.set_defaults(func=cmd_eval)

    s = sub.add_parser("check-gold", help="verify gold pages and evidence phrases against the extracted text")
    s.add_argument("--questions", type=_path_arg, help="questions file (default from config)")
    s.set_defaults(func=cmd_check_gold)
    return p


def _path_arg(value: str):
    from pathlib import Path
    return Path(value).expanduser().resolve()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RelayError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
