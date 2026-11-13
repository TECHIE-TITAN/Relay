# Deliverables checklist (updated as we go)

Legend: [x] done, [~] written, needs your run or check, [ ] not started. All items below are required (dashboard and README were promoted to required).

## A. Real-service runs
- [x] Real probe (single, latency, concurrency, limit) -> Table 1
- [x] Three pilots (untouched; checksums in `results/pilot_checksums.sha256`)
- [x] First S2 run (ladder on, contaminated; kept as evidence, superseded)
- [x] S2, S1, S3 quality runs, graded (grades proposed by the assistant, applied by the author)
- [x] S4 scored retrieval-only (Table 3b)

## B. Stand-in load runs
- [x] smoke, steady, burst, sustained_overload, limit_halved, limit_halved_hot, daily_exhausted, retry_naive, retry_budgeted, retry_naive_hot, retry_budgeted_hot

## C. Analysis
- [x] `analyze`: `results.csv` (899 rows), `results.sources.json`, `report/tables.md`

## D. Report
- [x] Content written, with the author's sections and Vaani text
- [~] 2-page PDF: compile `report/report_2page.tex` (Overleaf or pdflatex) or render the markdown to HTML and print; confirm exactly 2 pages
- [ ] `python -m relay.cli export-conversation`, then skim `report/conversation_history.md`
- [ ] No API key in the repo: `grep -rn "gsk_" --exclude-dir=.venv --exclude-dir=.git --exclude-dir=node_modules .` prints nothing

## E. Dashboard and README (required)
- [x] Backend `src/relay/dashboard_api.py` (read-only GET, SSE, provenance labels), tested
- [~] Dashboard v2 (five pages: Overview, Live Simulation, Experiments, Traces, Limits), in increments:
  - [~] 1. Backend: `src/relay/simulations.py`, simulation routes, `/api/rows`, `/api/runs/{id}/events`, live/interrupted detection (written; run `pytest -q`)
  - [~] 2. App shell with five pages (Live simulation is a placeholder) and the new Overview (nine KPI tiles, five charts, real/stand-in filter); `npm test` covers the aggregation maths
  - [~] 3. Live Simulation page (configuration form from the options endpoint, pipeline flow derived from the event stream, six live charts, request inspector with a link to the full trace, what-if pairs with a comparison table); `npm test` covers the event-to-state logic and the form arithmetic
  - [ ] 4. Traces lifecycle, Experiments (S4 retrieval-only), Limits (breaker, rate-limit events)
  - (the first frontend, four per-run views, exists in `dashboard/` and builds; it is replaced by increments 2 to 4)
- [~] `README.md` with reproduction steps: written; check the commands as you run them

## F. Final
- [ ] `pytest -q` green
- [ ] Final commit: `results/runs`, `results.csv`, `results.sources.json`, `report/` (with the PDF), `dashboard/` (not `node_modules` or `dist`), README; leave out `.claude/` and `test.sh`

## Known gaps stated in the report
- Requests in flight probed only up to 4; no limit found.
- Peak queue length is not a `results.csv` column (from the run manifests).
- The daily limit is SIMULATED; never triggered on the real service.
- TPM accounting (reservation vs actual) unresolved; the ledger assumes a UTC day reset while the headers suggest a rolling refill.
- Extractive fallback is weak; three post-pilot rule changes were made after two pilot samples; the ladder was off in the quality runs.
- 20 questions, one run per setting.
- Not built, deliberately: stage-priority/EDF scheduling, an LLM verifier.
