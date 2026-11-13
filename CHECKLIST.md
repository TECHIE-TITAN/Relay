# Deliverables checklist (updated as we go)

Legend: [x] done, [~] written / in progress, [ ] not started. P0 = must have, P1 = should have, P2 = if time allows.
Execution order (agreed): S2 alone -> grade S2 -> check ledger -> S1 -> grade -> check ledger -> S3 -> grade -> stand-in scenarios (nothing else running) -> `analyze` -> report -> dashboard.
Quota: ledger 22.5K of the 160K cap used before the S2 study; the caps S2 60K + S1 30K + S3 55K = 145K do NOT fit in the 137.5K left, so `run` refuses a cap that does not fit and `--token-cap N` lowers it. Check with `python -m relay.cli ledger`.

## A. Real-service runs (spend quota; the author runs them)
- [x] P0 Real probe: single, latency, concurrency, limit stages (`results/probe_real.jsonl`) -> Table 1
- [x] P0 Three pilot runs (kept untouched; checksums in `results/pilot_checksums.sha256`)
- [ ] P0 S2 quality study (`quality_S2`, cap 60K)
- [ ] P0 S1 quality study (`quality_S1`, cap 30K, lowered if the ledger requires)
- [ ] P0 S3 quality study (`quality_S3`, cap 55K, lowered if the ledger requires)
- [ ] P1 S4 scored retrieval-only (`eval-retrieval`); model metrics "not measured"
- [ ] P0 Manual grading of S2, S1, S3 (`grade-sheet --prefill-refusals`, fill `detail_grade`, `apply-grades`)

## B. Stand-in load runs (free; calibrated profile; run ALONE, after the real runs)
- [ ] P0 smoke, steady, burst, sustained_overload, limit_halved, daily_exhausted (SIMULATED)
- [ ] P0 retry_naive vs retry_budgeted, and the `_hot` versions
- [ ] P1 limit_halved_hot

## C. Analysis
- [~] P0 `analyze`: merges the latest run of each scenario + probe into `results.csv`; writes `results.sources.json` (run ids, checksums, runs left out and why); run folders are only read (tests written, not yet run)
- [~] P0 Tables 2a, 2b, 3, 3b in `report/tables.md`; Table 1 is written by hand in the report from the probe
- [~] P0 Slowest-request split (waiting / working / retrying, before or after first token) in `report/tables.md`
- [ ] P0 One number nobody could have guessed (candidates in the report skeleton)

## D. Report (2 pages) and submission pieces
- [~] P0 Report skeleton `report/report.md` with all headings in order; Table 1 and the naive-retriever section drafted from evidence; results sections wait for runs
- [ ] P0 Cut the report to 2 pages
- [ ] P0 Vaani feedback (YOURS)
- [~] P0 Conversation history: `export-conversation` written (tests written, not yet run); run it last so it includes everything
- [ ] P0 Three prompts that mattered, one thing the assistant got wrong, one overrule, what is distinctly yours (YOURS; candidates are in a comment at the end of the report)
- [ ] P0 No API key anywhere in the submission; `.env` is git-ignored
- [ ] P1 Dashboard (FastAPI read-only + React/TS/Tailwind/Recharts: Overview, Experiments, Traces, Limits); only after the evidence and report are in good shape
- [ ] P1 README with reproduction steps (README.md is currently only "# Relay")
- [ ] P0 Final commit including `results/runs` (raw results are required), `results.csv`, `results.sources.json`, `report/`

## Known gaps to state honestly in the report
- "Requests in flight at once" was only probed up to 4; no limit found there.
- Peak queue length is not a `results.csv` column (it comes from the run manifests); say so in Table 2b.
- Daily-limit behaviour is SIMULATED; never triggered on the real service by design.
- TPM accounting (reservation versus actual tokens) is unresolved: two observations disagreed.
- Extractive fallback is weak; three post-pilot rule changes (DESIGN.md section 16) were made after seeing two pilot samples.
- Stand-in runs made before the calibration (old TTFT and slowdown) are superseded by the reruns and left on disk.
