# Relay

A budgeted, rate-limit-aware question-answering pipeline over three pinned arXiv papers (Attention Is All You Need v7, Moshi v1, Dynamic Chunking v2), built for the "The Relay" take-home. It answers with citations or refuses, runs against the real Groq free tier (`openai/gpt-oss-20b`) and a stand-in model server I wrote, and logs every request so that every table in the write-up can be recomputed from `results.csv`.

**Start here:** the write-up is `report/report_2page.pdf` (sources `report/report_2page.md` and `report/report_2page.tex`; the long working version is `report/report.md`). Design decisions and their reasons are in `DESIGN.md`. The raw results are `results.csv` (one row per request), `results.sources.json` (which run each row came from, with checksums) and `results/runs/` (every run: manifest, event log, per-run results). `report/conversation_history.md` is the exported history with the assistant. `CHECKLIST.md` tracks deliverables.

## What you can reproduce without an API key

Everything except the real-service runs. The committed runs are enough to rebuild every table.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
python -m relay.cli analyze
python -m relay.cli render-report
```

`analyze` rebuilds `results.csv`, `results.sources.json` and `report/tables.md` from `results/runs/` and `results/probe_real.jsonl`. It only reads the run folders. Python 3.9 or newer.

## The pipeline from scratch (papers, chunks, retrieval)

```bash
python -m relay.cli config
python -m relay.cli ingest
python -m relay.cli extract
python -m relay.cli chunk
python -m relay.cli check-gold
python -m relay.cli eval-retrieval --settings S1 S2 S3 S4 --no-save
python -m relay.cli search --question-id q01 --setting S2
```

`ingest` downloads the three papers at the exact versions in `config/relay.yaml` and verifies the version stamp. The 20 questions and their gold pages are in `data/questions.yaml` (written by me); gold changes are logged in `data/gold_changes.md`. Retrieval is my own BM25 (`src/relay/bm25.py`) over four chunking settings S1 to S4.

## Load scenarios on the stand-in (free)

```bash
python -m relay.cli list-scenarios
python -m relay.cli run --scenario steady --spawn-standin
```

Scenarios: `smoke`, `steady`, `burst`, `sustained_overload`, `limit_halved`, `limit_halved_hot`, `daily_exhausted`, `retry_naive`, `retry_budgeted`, `retry_naive_hot`, `retry_budgeted_hot`. Each writes `results/runs/<scenario>_<timestamp>/` with `manifest.json`, `events.jsonl` and `results.csv`. Run them with nothing else busy on the machine. The stand-in's limits are the documented limits times 5 (SIMULATED, recorded in each manifest); its time to first token is calibrated from the real probe.

## Real-service runs (spend quota; need a key)

Put your own key in `.env` (copy `.env.example`; never commit it). Every real command needs `--allow-real` and is checked against a client-side daily ledger.

```bash
python -m relay.cli ledger
python -m relay.cli probe --stage single --allow-real
python -m relay.cli probe --stage latency --allow-real
python -m relay.cli probe --stage concurrency --allow-real
python -m relay.cli probe --stage limit --allow-real
python -m relay.cli run --scenario quality_S2 --allow-real
python -m relay.cli run --scenario quality_S1 --allow-real
python -m relay.cli run --scenario quality_S3 --allow-real
```

What I spent: about 129,000 tokens of the 200,000 daily limit (probe about 12K, three pilots about 10K, one contaminated first S2 run 21K, then S2 29.5K, S1 15.3K, S3 41.2K). A run is refused before anything is sent if its token cap does not fit in what the ledger has left; `--token-cap N` can only lower a cap. The daily limit was never triggered on purpose, so the daily scenario is simulated.

## Grading the quality runs

```bash
python -m relay.cli grade-sheet --run RUN_ID --prefill-refusals
python -m relay.cli apply-grades --run RUN_ID --proposals results/runs/RUN_ID/grades_proposed.csv
```

`grade-sheet` writes `grades.csv` for you to fill in (`detail_grade`: correct, partial, incorrect, wrong_refusal, correct_refusal, answered_unanswerable). `apply-grades` maps those to the brief's four values (correct, wrong, refused_correctly, refused_wrongly) and records who proposed what. In the submitted runs the grades were proposed by the assistant and applied by me.

## Dashboard

React, TypeScript, Tailwind and Recharts over a FastAPI process. It shows the runs, a live view of a running scenario over SSE, the experiment tables, request traces and the limits, with documented, observed and simulated numbers kept apart. History is read-only. The only write is starting a **stand-in** simulation from the Live Simulation page: the API builds an ordinary scenario from an allowlisted form and launches the existing runner (`python -m relay.cli run ... --spawn-standin`) as a subprocess, with no `--allow-real` and every `GROQ*` variable removed from its environment, so it cannot call the provider or use quota. Those runs are named `sim_*`, are git-ignored and are never merged into `results.csv`. `--read-only` turns the simulation routes off.

```bash
python -m relay.cli dashboard-api
```

In a second terminal:

```bash
cd dashboard
npm install
npm run dev
```

Open http://localhost:5173. To watch a live run, start any scenario in a third terminal and select it under Traces. `npm run build` type-checks and builds; `npm test` runs the unit tests of the dashboard's aggregation maths.

## Layout

| Path | What it is |
|---|---|
| `src/relay/` | the pipeline: `pipeline.py`, `gateway.py` (token/request windows, retries, ledger), `admission.py` (bounded queue), `verify.py` (V0 checks and extractive fallback), `llm_client.py`, `standin/` (the stand-in server), `runner.py` (scenarios), `metrics.py`, `analyze.py`, `grading.py`, `dashboard_api.py` |
| `config/` | `relay.yaml` (papers, model, retrieval settings), `scenarios.yaml`, `limits.yaml` (documented limits with provenance) |
| `data/` | questions and gold, chunk files, gold-change log |
| `results/` | `runs/` (raw), `probe_real.jsonl`, `retrieval_eval/`, the pilot checksums |
| `report/` | the write-up, `tables.md`, `authorship_evidence.md`, the conversation history |
| `dashboard/` | the frontend |
| `tests/` | about 270 tests; none calls the real service |

## Known limits

The stand-in's decode speed, failure rates and tail are my choices (SIMULATED). Requests in flight were only probed up to 4. Whether the provider charges the reservation or the actual tokens is unresolved. The verifier is word overlap, not entailment. 20 questions, one run per setting, so differences between settings are indicative. DESIGN.md sections 12 to 17 list each decision and its limits.
