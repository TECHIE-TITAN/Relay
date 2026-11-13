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
  standin         run the stand-in model server (SIMULATED behaviour, no network beyond localhost)
  list-scenarios  show the scenarios in config/scenarios.yaml
  run             run a load scenario (stand-in by default; real needs --allow-real)
  summarize       print the summary and invariants of a finished run

Only `run` with a scenario whose source is `real` and --allow-real can call the model provider.
Everything else, including every stand-in run, stays on this machine (ingest alone contacts arxiv.org).
"""
from __future__ import annotations

import argparse
import asyncio
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
    query = args.query
    if args.question_id:
        # Take the text straight from the questions file: no quoting or typing errors, and no API call.
        qs = {q.id: q for q in load_questions(cfg.paths.questions, [p.key for p in cfg.papers])}
        if args.question_id not in qs:
            raise RelayError(f"unknown question id '{args.question_id}'")
        query = qs[args.question_id].text
        print(f"question {args.question_id}: {query}")
    if not query:
        raise RelayError("give a query or --question-id")
    res = retriever.search(query, paper_keys=args.paper, k=args.k)
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


def _fmt(v: object, nd: int = 0) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _dist_line(label: str, d: dict) -> str:
    line = (f"{label:<18} n={d['n']:<4} p50={_fmt(d['p50'])} p95={_fmt(d['p95'])} "
            f"p99={_fmt(d['p99'])} max={_fmt(d['max'])}")
    notes = [x for x in (d.get("note"), d.get("p99_note")) if x]
    return line + ("   [" + "; ".join(notes) + "]" if notes else "")


def format_summary(manifest: dict, summary: dict, invariants: list) -> str:
    sc = summary["status_counts"]
    n = summary["n_requests"]
    pct = lambda k: f"{100.0 * sc[k] / n:.1f}%" if n else "n/a"   # noqa: E731
    lines = [
        f"run {manifest.get('run_id')}   scenario {summary['scenario']}   source {summary['source']}",
        f"provenance: {summary['provenance']}",
        f"requests {n}   ok {sc['ok']} ({pct('ok')})   rate_limited {sc['rate_limited']} ({pct('rate_limited')})   "
        f"timeout {sc['timeout']} ({pct('timeout')})   error {sc['error']} ({pct('error')})   "
        f"unfinished {summary['n_incomplete']}",
        f"ok answers by mode {summary['ok_by_answer_mode']}   extractive (degraded) {summary['degraded_ok']}   "
        f"shed at door {summary['shed_reasons']}",
        _dist_line("first_token_ms (ok)", summary["first_token_ms"]),
        _dist_line("first_token (model)", summary["first_token_ms_llm"]),
        _dist_line("total_ms (model)", summary["total_ms_llm"]),
        _dist_line("total_ms (ok)", summary["total_ms_ok"]),
        _dist_line("total_ms (all)", summary["total_ms_all"]),
        f"goodput/min {_fmt(summary['goodput_per_min'], 1)}   attempts {summary['attempts_total']}   "
        f"retries {summary['retries_total']} (in {summary['requests_with_retries']} requests)   "
        f"discretionary calls {summary['discretionary_calls_total']}   regenerations {summary['regenerations']}",
        f"tokens in {summary['tokens_in']}  out {summary['tokens_out']}  per answer {_fmt(summary['tokens_per_answer'])}  "
        f"unaccounted est (failed calls) {summary['tokens_unaccounted_est']}",
        f"notional cost per 1000 answers (USD, {manifest.get('prices', {}).get('provenance')}): "
        f"{_fmt(summary['notional_cost_per_1000_answers_usd'], 4)}",
        f"peak queue {_fmt(summary['peak_queue_len'])}   peak in flight {_fmt(summary['peak_inflight'])}   "
        f"max launch lag ms {_fmt(summary.get('max_launch_lag_ms'), 1)}",
        "invariants:",
    ]
    for r in invariants:
        state = "n/a" if not r["applicable"] else ("PASS" if r["passed"] else f"FAIL ({r['n_violations']})")
        lines.append(f"  {r['id']} {state:<9} checked {r['checked']:<5} {r['description']}")
    if manifest.get("source") == "stand-in":
        lines.append("NOTE: stand-in run. Latency and limit numbers describe the stand-in's parameters; no quality number may be taken from it.")
    return "\n".join(lines)


def cmd_standin(args: argparse.Namespace) -> int:
    from .standin.server import main as standin_main

    argv = ["--host", args.host, "--port", str(args.port), "--seed", str(args.seed),
            "--limits-scale", str(args.limits_scale)]
    for s in args.set or []:
        argv += ["--set", s]
    return standin_main(argv)


def cmd_list_scenarios(args: argparse.Namespace) -> int:
    from .runner import load_scenarios, resolve_scenario

    cfg = load_config()
    path = args.scenarios_file or cfg.root / "config" / "scenarios.yaml"
    doc = load_scenarios(path)
    for name in doc["scenarios"]:
        s = resolve_scenario(doc, name)
        print(f"{name:<20} {s.get('source', 'standin'):<8} {s.get('description', '')}")
    return 0


def _ledger(cfg):
    from .gateway import DailyLedger, GatewayConfig
    path = cfg.paths.results_dir / f"ledger_{cfg.model().id.replace('/', '_')}.json"
    g = GatewayConfig()
    return DailyLedger(g.daily_token_cap, g.daily_request_cap, path=path), path


def _check_ledger_fits(cfg, scn) -> None:
    """A real run's token cap must fit in what is left of the client-side daily ledger, or it is refused BEFORE
    anything is sent. (Several runs whose caps add up to more than the day must not all be allowed their full cap.)"""
    cap = (scn.get("gateway") or {}).get("pilot_token_cap")
    if cap is None:
        return
    ledger, _ = _ledger(cfg)
    left = ledger.token_cap - ledger.tokens
    print(f"ledger today: {ledger.tokens}/{ledger.token_cap} tokens, {ledger.requests}/{ledger.request_cap} requests; "
          f"this run's cap {cap}; worst case after the run {ledger.tokens + int(cap)}")
    if ledger.tokens + int(cap) > ledger.token_cap:
        raise RelayError(
            f"this run's cap ({cap}) does not fit in the daily ledger ({ledger.tokens} used of {ledger.token_cap}, {left} left). "
            f"Nothing was sent. Re-run with a lower cap, for example --token-cap {max(0, left)}.")


def cmd_ledger(args: argparse.Namespace) -> int:
    cfg = load_config()
    ledger, path = _ledger(cfg)
    print(f"date (UTC, an assumption): {ledger.today}")
    print(f"tokens   {ledger.tokens} / {ledger.token_cap}  ({ledger.token_cap - ledger.tokens} left under the cap)")
    print(f"requests {ledger.requests} / {ledger.request_cap}")
    print(f"provider daily limit hit: {ledger.provider_daily_limit_hit}")
    print(f"file: {path}")
    print("This counts only calls made by this tool. The provider's own daily total (200K tokens, 1,000 requests) may be higher.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .runner import load_scenarios, resolve_scenario, run_scenario, spawn_standin

    cfg = load_config()
    path = args.scenarios_file or cfg.root / "config" / "scenarios.yaml"
    scn = resolve_scenario(load_scenarios(path), args.scenario)
    if args.setting:
        cfg.setting(args.setting)           # validates
        scn["setting"] = args.setting
    if args.token_cap is not None:          # may only LOWER the scenario's cap, never raise it
        gw = dict(scn.get("gateway") or {})
        old = gw.get("pilot_token_cap")
        gw["pilot_token_cap"] = args.token_cap if old is None else min(int(old), args.token_cap)
        scn["gateway"] = gw
    if scn.get("source") == "real":
        _check_ledger_fits(cfg, scn)
    url = args.standin_url
    proc = None
    if args.spawn_standin and scn.get("source", "standin") == "standin":
        proc = spawn_standin(args.port)
        url = f"http://127.0.0.1:{args.port}"
    try:
        res = asyncio.run(run_scenario(cfg, scn, run_id=args.run_id, standin_url=url,
                                       allow_real=args.allow_real, scenarios_file=str(path)))
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
    from .artifacts import read_manifest

    print(format_summary(read_manifest(res.paths.manifest), res.summary, res.invariants))
    print(f"\nartifacts: {res.paths.root}")
    return 0


def cmd_summarize(args: argparse.Namespace) -> int:
    from .artifacts import list_runs, read_events, read_manifest, run_paths
    from .metrics import check_invariants, request_rows, summarize

    cfg = load_config()
    run_id = args.run
    if run_id == "latest":
        runs = list_runs(cfg.paths.results_dir)
        if not runs:
            raise RelayError("no runs found under results/runs")
        run_id = runs[-1]
    paths = run_paths(cfg.paths.results_dir, run_id)
    manifest = read_manifest(paths.manifest)
    events = read_events(paths.events)
    rows = request_rows(events, manifest)
    summary = summarize(rows, events, manifest)
    lags = [e["lag_ms"] for e in events if e["event"] == "launch"]
    summary["max_launch_lag_ms"] = max(lags) if lags else None
    invariants = check_invariants(events, manifest.get("invariant_params") or {})
    print(format_summary(manifest, summary, invariants))
    return 0


def _graded_run(args: argparse.Namespace):
    """Resolve an explicitly named run (no 'latest': grading must never land on the wrong run by accident)."""
    from .artifacts import read_events, read_manifest, run_paths
    from .eval_retrieval import load_questions as _load_q
    cfg = load_config()
    paths = run_paths(cfg.paths.results_dir, args.run)
    if not paths.manifest.is_file():
        raise RelayError(f"run '{args.run}' not found under results/runs")
    manifest = read_manifest(paths.manifest)
    questions = {q.id: q for q in _load_q(cfg.paths.questions, [p.key for p in cfg.papers])}
    return cfg, paths, manifest, read_events(paths.events), questions


def cmd_grade_sheet(args: argparse.Namespace) -> int:
    from .grading import build_sheet, write_sheet
    _, paths, manifest, events, questions = _graded_run(args)
    sheet_path = paths.root / "grades.csv"
    if sheet_path.exists() and not args.force:
        raise RelayError(f"{sheet_path} already exists (your grades may be in it). Use --force to overwrite.")
    rows = build_sheet(manifest, events, questions, prefill_refusals=args.prefill_refusals)
    write_sheet(sheet_path, rows)
    print(f"wrote {sheet_path} with {len(rows)} rows. Fill the detail_grade column, then run apply-grades.")
    print("detail_grade values: correct, partial, incorrect (answerable, answered); wrong_refusal (answerable, refused);")
    print("                     correct_refusal (unanswerable, refused); answered_unanswerable (unanswerable, answered)")
    if manifest.get("incomplete"):
        print(f"NOTE: this run is INCOMPLETE ({(manifest.get('stop') or {}).get('reason')}); "
              f"{len((manifest.get('stop') or {}).get('not_run') or [])} questions were not run.")
    return 0


def cmd_apply_grades(args: argparse.Namespace) -> int:
    import json
    from .artifacts import write_manifest
    from .grading import apply_grades, check_gradable_run, merge_proposals, read_sheet, write_sheet
    _, paths, manifest, _events, _questions = _graded_run(args)
    check_gradable_run(manifest)
    sheet = read_sheet(paths.root / "grades.csv")
    if args.proposals:
        sheet, n = merge_proposals(sheet, args.proposals)
        write_sheet(paths.root / "grades.csv", sheet)        # grades.csv keeps the final detail, with the proposals marked
        print(f"filled {n} blank grades from {args.proposals.name}, marked 'assistant-proposed' in the note column")
    block = apply_grades(paths.results, sheet, allow_partial=args.allow_partial)
    write_manifest(paths.manifest, {**manifest, "grading": block})
    print(f"graded {block['n_graded']} answers ({block['n_ungraded_ok']} still ungraded)")
    print("graded by:", block["graded_by"])
    print("answer_grade:", json.dumps(block["answer_grade_counts"], sort_keys=True))
    print("by answer mode and detail:", json.dumps(block["by_mode_and_detail"], sort_keys=True))
    print(f"results.csv updated; the original is kept as {paths.root / 'results.ungraded.csv'}")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    from .analyze import analyze
    cfg = load_config()
    m = cfg.model()
    desc = {}
    for sid, s in cfg.settings.items():
        rerank = "no re-rank" if s.rerank == "none" else f"re-rank {s.rerank} (fetch {s.fetch_k})"
        desc[sid] = f"{s.chunk_tokens}-token pieces, {s.k} fetched, {rerank}"
    out_csv = args.csv or (cfg.root / "results.csv")
    out_md = args.md or (cfg.root / "report" / "tables.md")
    res = analyze(cfg.paths.results_dir, out_csv, out_md, probe_path=cfg.paths.results_dir / "probe_real.jsonl",
                  prices={"in": m.price_in_per_mtok, "out": m.price_out_per_mtok}, settings_desc=desc,
                  eval_dir=cfg.paths.results_dir / "retrieval_eval")
    print(f"wrote {out_csv} ({res['n_rows']} rows), {out_md} and {res['sources_file']}")
    print("run folders were only read, never modified; the sources file lists every run used, its checksums and the runs left out")
    for sc, rid in sorted(res["runs"].items()):
        print(f"  {sc:22s} <- {rid}")
    for w in res["warnings"]:
        print(f"WARNING: {w}")
    return 0


def cmd_export_conversation(args: argparse.Namespace) -> int:
    from .export_conversation import export, sessions_dir_for
    cfg = load_config()
    d = args.sessions_dir or sessions_dir_for(cfg.root)
    files = sorted(d.glob("*.jsonl")) if d.is_dir() else []
    if not files:
        raise RelayError(f"no session files (*.jsonl) in {d}. Pass --sessions-dir.")
    out = args.out or (cfg.root / "report" / "conversation_history.md")
    res = export(files, out, max_tool_chars=args.max_tool_chars, only_sessions=args.session)
    print(f"wrote {out}")
    print(json.dumps({k: v for k, v in res.items() if k != "out"}, indent=1))
    print("Review the file before submitting: secrets are redacted by pattern only.")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    from .gateway import DailyLedger, GatewayConfig
    from .llm_client import LLMClient
    from .probe import PLANNED_TOKENS, ProbeEnv, run_probe

    cfg = load_config()
    model = cfg.model()
    real = not args.base_url
    key = None
    if real:
        if not args.allow_real:
            raise RelayError("a probe calls the REAL service and spends quota. Re-run with --allow-real. Nothing was sent.")
        key = get_api_key(cfg.root)
        if not key:
            raise RelayError("GROQ_API_KEY is not set (put it in .env). Nothing was sent.")
    g = GatewayConfig()
    ledger = DailyLedger(g.daily_token_cap, g.daily_request_cap,
                         path=(cfg.paths.results_dir / f"ledger_{model.id.replace('/', '_')}.json") if real else None)
    print(f"probe stage '{args.stage}' on {'REAL ' + model.id if real else 'stand-in ' + args.base_url}")
    print(f"planned: at most about {PLANNED_TOKENS[args.stage]} tokens. Ledger today: {ledger.tokens}/{ledger.token_cap} tokens, "
          f"{ledger.requests} requests (client-side count of this tool's own calls).")

    async def go() -> list:
        client = LLMClient(args.base_url or model.base_url, model.id, api_key=key,
                           extra_params={} if args.no_extra_params else model.extra_params,
                           stream_options=args.stream_options)
        env = ProbeEnv(client, ledger, cfg.paths.results_dir / "probe_real.jsonl", model.id, "real" if real else "stand-in")
        try:
            return await run_probe(env, args.stage, n=args.n, big=args.big, reps=args.reps,
                                   max_calls=args.max_calls, recovery=not args.no_recovery)
        finally:
            await client.aclose()

    for line in asyncio.run(go()):
        print(line)
    print(f"ledger now: {ledger.tokens}/{ledger.token_cap} tokens, {ledger.requests} requests")
    print(f"raw rows appended to {cfg.paths.results_dir / 'probe_real.jsonl'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="relay", description="The Relay: command line tools")
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
    s.add_argument("query", nargs="?", help="query text (or use --question-id)")
    s.add_argument("--question-id", help="use the text of this question from the questions file (offline, no API)")
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

    s = sub.add_parser("standin", help="run the stand-in model server (SIMULATED)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8089)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--limits-scale", type=float, default=1.0)
    s.add_argument("--set", action="append", help="override a profile field, KEY=VALUE (repeatable)")
    s.set_defaults(func=cmd_standin)

    s = sub.add_parser("list-scenarios", help="show the scenarios")
    s.add_argument("--scenarios-file", type=_path_arg)
    s.set_defaults(func=cmd_list_scenarios)

    s = sub.add_parser("run", help="run a load scenario")
    s.add_argument("--scenario", required=True)
    s.add_argument("--scenarios-file", type=_path_arg)
    s.add_argument("--standin-url", default="http://127.0.0.1:8089", help="stand-in server (default %(default)s)")
    s.add_argument("--spawn-standin", action="store_true", help="start a stand-in server for this run and stop it afterwards")
    s.add_argument("--port", type=int, default=8089, help="port for --spawn-standin")
    s.add_argument("--setting", help="override the scenario's retrieval setting")
    s.add_argument("--run-id")
    s.add_argument("--allow-real", action="store_true", help="REQUIRED for a scenario that calls the real service (spends quota)")
    s.add_argument("--token-cap", type=int, help="real runs: lower this run's token cap (never raises the scenario's cap)")
    s.set_defaults(func=cmd_run)

    sub.add_parser("ledger", help="show today's client-side token and request ledger for the real service").set_defaults(func=cmd_ledger)

    s = sub.add_parser("probe", help="probe the REAL service in small stages (spends quota)")
    s.add_argument("--stage", required=True, choices=["single", "latency", "concurrency", "limit"])
    s.add_argument("--allow-real", action="store_true", help="REQUIRED: this calls the real service")
    s.add_argument("--n", type=int, default=10, help="latency: number of calls")
    s.add_argument("--big", type=int, default=4, help="latency: how many of them use a ~1.5K-token prompt")
    s.add_argument("--reps", type=int, default=3, help="concurrency: batches per level")
    s.add_argument("--max-calls", type=int, default=40, help="limit: stop after this many calls even without a 429")
    s.add_argument("--no-recovery", action="store_true", help="limit: do not wait for recovery after the first 429")
    s.add_argument("--no-extra-params", action="store_true", help="omit extra_params (e.g. reasoning_effort) from the request")
    s.add_argument("--stream-options", action="store_true", help="send stream_options.include_usage")
    s.add_argument("--base-url", help="TEST HOOK: probe this OpenAI-compatible URL (a local stand-in) instead of the real service")
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("grade-sheet", help="export a grading sheet for a finished REAL quality run")
    s.add_argument("--run", required=True, help="run id (explicit; pilots and stand-in runs are refused)")
    s.add_argument("--force", action="store_true", help="overwrite an existing grades.csv")
    s.add_argument("--prefill-refusals", action="store_true",
                   help="fill in the grade of refusals (mechanical: follows from answerable or not); answers stay blank")
    s.set_defaults(func=cmd_grade_sheet)

    s = sub.add_parser("apply-grades", help="merge your grades.csv into the run's results.csv (answer_grade)")
    s.add_argument("--run", required=True)
    s.add_argument("--allow-partial", action="store_true", help="allow answers that are still ungraded")
    s.add_argument("--proposals", type=_path_arg,
                   help="CSV (request_id, detail_grade, note) of proposed grades; fills blank cells only and marks them assistant-proposed")
    s.set_defaults(func=cmd_apply_grades)

    s = sub.add_parser("export-conversation", help="export the real assistant conversation history to report/conversation_history.md")
    s.add_argument("--sessions-dir", type=_path_arg, help="folder with the session *.jsonl files (default: this project's folder under ~/.claude/projects)")
    s.add_argument("--out", type=_path_arg)
    s.add_argument("--max-tool-chars", type=int, default=1500, help="clip tool inputs and outputs to this many characters")
    s.add_argument("--session", action="append", help="only this session id (repeatable); default all")
    s.set_defaults(func=cmd_export_conversation)

    s = sub.add_parser("analyze", help="merge the runs into results.csv and write the brief's tables (report/tables.md)")
    s.add_argument("--csv", type=_path_arg, help="output results.csv (default: project root)")
    s.add_argument("--md", type=_path_arg, help="output tables markdown (default: report/tables.md)")
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("summarize", help="summary and invariants of a finished run")
    s.add_argument("--run", default="latest", help="run id, or 'latest'")
    s.set_defaults(func=cmd_summarize)
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
