# The Relay: Architecture and Implementation Plan

Status: design only. No code written, nothing run, no API calls made.
Everything marked **[VERIFY]** is a hypothesis from memory that must be confirmed from Groq's docs or measured. Nothing marked that way should reach the write-up as fact.

---

## 0. Framing: what this system really is

The brief looks like a RAG assignment, but the real problem is a **budgeted, latency-sensitive, multi-call pipeline on a rate-limited upstream**. Three facts drive the whole design:

1. **Calls per answer is the currency.** If the provider allows R requests/min and each answer needs C model calls, sustainable throughput is `R / C` answers/min. Cutting C from 4 to 2 doubles capacity, which beats any amount of clever retrying.
2. **Free-tier limits are several independent budgets** (RPM, TPM, RPD, TPD, maybe concurrency). Whichever is tightest for a given request mix is the real limit, and it can differ per model.
3. **Under overload, wasted work kills you.** If 100 requests each finish step 1 and none reach step 5, you spent quota and delivered nothing. The scheduler must favour finishing over starting.

The one decision I expect to shape the design most (candidate for write-up heading 1):
**a budget-aware degradation ladder with admission control**, i.e. the pipeline can shed optional LLM calls per request based on live budget, and never sheds citation or refusal safety.

---

## 1. Technology choices (with alternatives)

| Concern | Choice | Alternatives | Why / cost |
|---|---|---|---|
| Language | **Python 3.12 + asyncio** | Go (best concurrency, fast), C++ (no) | Fastest to build in 6-8h, best PDF/retrieval ecosystem. Cost: GIL, but workload is I/O-bound; load generator at ~hundreds req/s is fine. Go would be cleaner for the limiter but retrieval libraries are weaker. |
| HTTP client | **httpx (async) with streaming** | aiohttp, official `groq` SDK | httpx gives me control over timeouts, raw headers, and TTFT timing. The SDK hides retries/headers; the SDK also retries silently, which would corrupt my retry measurements. If using the SDK, set `max_retries=0`. |
| API shape | **OpenAI-compatible `/chat/completions` with SSE** | Groq-native | Groq exposes an OpenAI-compatible API **[VERIFY]**. The same client code then targets real Groq or my stand-in by changing a base URL. This is the main fidelity guarantee. |
| Stand-in server | **FastAPI + uvicorn** | aiohttp server, Go, raw asyncio | FastAPI `StreamingResponse` makes SSE easy. Runtime control endpoint for changing behaviour mid-run. |
| PDF text | **PyMuPDF (fitz)** | pdfplumber, pypdf, arXiv LaTeX source (e-print), GROBID | PyMuPDF is fast and gives page numbers, which I need for citations. LaTeX source gives cleaner math/tables but needs a TeX parser and loses page numbers. Tradeoff accepted: some table mangling (see section 6.4). |
| Lexical retrieval | **BM25 (`rank_bm25` or ~40 lines own)** | TF-IDF, SQLite FTS5 | Free, zero latency, good on exact terms like "multi-head" or "Mimi". |
| Dense retrieval | **Local `sentence-transformers` small model (e.g. MiniLM-class)** | Hosted embeddings (uses quota, no), no dense at all | Local means zero provider quota and no extra latency risk. Cost: first-run model download and CPU embed time (corpus is tiny, seconds). |
| Fusion | **Reciprocal Rank Fusion** | weighted score sum | Avoids score-scale calibration. |
| Re-ranking | **Optional: local cross-encoder** | LLM rerank (costs quota, rejected), none | Treated as an experimental setting, not default. |
| Storage | **Plain files: chunks.jsonl, index pickle, results.csv, events.jsonl** | SQLite, Postgres | Reviewers read files. No DB to explain. |
| Observability | **Own structured event log (JSONL) with monotonic clock** | OpenTelemetry, Prometheus | The write-up needs per-request span breakdown (heading 4). A custom log is simpler than wiring OTel and is exactly computable. |
| Load generator | **Own async open-loop generator** | locust, k6, wrk | Existing tools don't know about my pipeline's patience/give-up semantics or token counting. Own generator is ~100 lines. |
| Analysis | **pandas + a single `analyze.py`** | spreadsheet | Tables must be reproducible from results.csv, so one script generates every table. |
| Config | **One YAML/TOML + `.env` for key** | env vars only | Scenarios and limiter parameters live in config for reproducibility. Key in `.env`, git-ignored. |

**Model choice [VERIFY]:** Groq free tier hosts several models with different limits (typically a small fast one like a Llama 3.1 8B instant and a larger 70B-class one). Plan: **small model for classify/verify, larger for draft** only if quota allows; otherwise single small model everywhere. Decide after reading the limits page and probing, because the tightest limit per model decides this.

---

## 2. System architecture

```
            ┌────────────────────────────── Relay process (asyncio) ───────────────────────────┐
 load gen   │                                                                                  │
 (open-loop)│  ┌───────────┐   ┌────────────┐   ┌─────────────────────────┐   ┌─────────────┐   │
 ──────────▶│  │ Admission │──▶│ Coalescing │──▶│ Pipeline orchestrator   │──▶│ Result sink │   │
  requests  │  │ + deadline│   │ + cache    │   │ (per-request state mc)  │   │ results.csv │   │
            │  └─────┬─────┘   └────────────┘   └───────────┬─────────────┘   │ events.jsonl│   │
            │        │ shed                                 │                 └─────────────┘   │
            │        ▼                                      ▼                                   │
            │  "busy / try later"                 ┌───────────────────┐   ┌──────────────────┐  │
            │                                     │ LLM gateway       │   │ Retrieval tool   │  │
            │                                     │ - model buckets   │   │ (local, timeouts │  │
            │                                     │ - RPM/TPM buckets │   │  and slow-tool   │  │
            │                                     │ - concurrency cap │   │  injection)      │  │
            │                                     │ - daily budget    │   └──────────────────┘  │
            │                                     │ - retry policy    │                         │
            │                                     │ - circuit breaker │                         │
            │                                     └─────────┬─────────┘                         │
            └───────────────────────────────────────────────┼───────────────────────────────────┘
                                                            │ base_url switch
                                         ┌──────────────────┴─────────────────┐
                                         ▼                                    ▼
                                  Real Groq API                  Stand-in server (FastAPI)
```

Key structural rule: **every model call goes through the one LLM gateway.** The pipeline never talks to HTTP directly. That gives a single place for rate limiting, retries, accounting, and tracing, and makes the real/stand-in switch a config line.

### 2.1 Components

- **Admission controller:** decides at arrival whether to accept, based on queue depth and estimated wait vs the request's deadline.
- **Coalescer / cache:** identical (normalized) in-flight questions share one execution (single-flight); finished answers go to an LRU cache.
- **Orchestrator:** an explicit state machine per request (not an agent framework; see alternatives).
- **LLM gateway:** the heart of the system (section 4).
- **Retrieval tool:** local index; wrapped as a "tool" with its own timeout so tool failures are a real failure mode.
- **Result sink:** the only writer of results.csv (single writer avoids interleaved rows).

### 2.2 Orchestrator: framework or hand-rolled?

| Option | Pro | Con |
|---|---|---|
| **Hand-rolled async state machine (chosen)** | Total control of retries, per-stage priority, token accounting; nothing hidden; easy to defend in the live interview. | More code. |
| LangChain / LlamaIndex | Fast scaffold | Hidden retries and extra calls corrupt measurements; hard to explain; brief explicitly wants *my* choices. |
| Tool-calling agent loop (model decides tools) | Most "agentic" | Unbounded calls per request; unpredictable cost and latency; terrible fit for a rate-limited budget. |
| Static DAG | Predictable | Cannot satisfy "at least one branch or loop" naturally. |

Chosen: a **bounded state machine** where branches are decided by deterministic checks where possible. The model is not trusted to control call count.

---

## 3. Request lifecycle

Five logical steps. Call counts are the point.

```
 arrive ─▶ [0 admit/coalesce/cache] ─▶ [1 understand] ─▶ [2 retrieve (TOOL)] ─▶ [3 decide: branch/loop]
                                                                                    │
                          ┌─────────────── refuse (no evidence) ◀───────────────────┤
                          │                                                         │ evidence ok
                          │               (loop once: reformulate + retrieve) ◀─────┤ weak
                          ▼                                                         ▼
                   refusal reply                                            [4 draft, streamed, cited]
                                                                                    ▼
                                                                            [5 verify] ─▶ send
                                                                                    │ fail
                                                                                    ▼ regenerate once or refuse
```

| Step | Implementation | LLM calls | Notes |
|---|---|---|---|
| 0 Admit | deadline set (e.g. 20 s to give up **[tunable]**), queue check, cache/coalesce | 0 | Shedding happens here, cheaply, before spending quota. |
| 1 Understand | Rule-based first: normalize, detect obvious paper names, extract keywords. **LLM rewrite only if the question is ambiguous/short** (cheap heuristic) | 0 or 1 | Alternative: always call LLM (cleaner, costs 1 call per answer). Measured later as a retrieval setting. |
| 2 Retrieve (tool) | hybrid BM25+dense, top-k, optional rerank, per-tool timeout (e.g. 2 s) | 0 | Fully local, so no provider quota. Failure injection here covers "tools time out". |
| 3 Decide | **Deterministic**: evidence score = top fused score + score margin + lexical overlap. Branches: *enough* → draft; *weak* → one reformulate+retrieve loop (max 1) → re-evaluate; *none* → correct refusal without any LLM call | 0 (or 1 for reformulation) | Refusing without an LLM call is both the cheapest and the safest path for unanswerable questions. The risk is false refusals (grade `refused_wrongly`), which is a measured tradeoff. |
| 4 Draft | LLM, **streamed**, prompt forces `[paper §section p.N]` citations, instruction to say "the papers do not say" if unsupported. `max_tokens` capped | 1 | TTFT of the user-visible reply is measured here. |
| 5 Verify | **Tier V0 (mandatory, never shed, no provider quota):** see section 3.2. **Tier V1 (optional LLM entailment check):** only when budget is healthy | 0 or 1 | V0 failure: regenerate once with a stricter prompt (loop), else extractive fallback or refuse. An answer that has not passed V0 is never released, at any pressure level. |

**Typical call count: 1 (draft) to 4 (rewrite + reformulate + draft + LLM verify).** The ladder in section 5.3 chooses which optional calls to spend.

### 3.2 Verification tiers

**V0, mandatory. Runs on every answer at every pressure level.** All checks are local, so they cost no provider quota and only milliseconds.

| # | Check | How | Catches |
|---|---|---|---|
| 1 | Citation validity | every cited chunk id / paper / page exists *and* was in this request's retrieved set | invented or stale citations |
| 2 | Citation coverage | every factual sentence carries at least one citation; only refusal and hedge sentences are exempt | uncited claims |
| 3 | Quote fidelity | any quoted span appears verbatim (after whitespace/hyphen normalization) in the cited chunk | fabricated quotes |
| 4 | Number and entity grounding | every number, unit, and named entity in a sentence appears in its cited chunk | wrong figures such as BLEU or layer counts |
| 5 | Sentence-level support (unsupported-claim check) | per sentence, support score against its cited chunk, from a **local** NLI/cross-encoder model, with lexical overlap as the fallback if the model is unavailable. Below threshold means the claim is flagged. | claims the chunk does not support |
| 6 | Refusal consistency | a refusal must not coexist with substantive uncited claims; an answer must not contradict its own hedge | half-refusals |

Outcome on failure: regenerate once (costs one call, so it is accounted for in the budget). If that fails, or if budget does not allow a regeneration, use the extractive fallback (verbatim passages pass V0 by construction), otherwise refuse honestly. **Never release an answer that failed V0.**

**V1, optional:** an LLM entailment check over the whole answer. It is the expensive tier, with one extra call, and is the only verification that is shed under pressure. It adds semantic robustness beyond V0, for example subtle paraphrase that flips meaning.

Honest costs of this split, to measure and report:
- V0 check 5 is only as good as the local model. Too strict means false rejections of correct paraphrases, so more regenerations, which use quota *under load* when quota is scarcest. Too lax lets unsupported claims through. The threshold is tuned on the question set, and the V0 reject rate belongs in the results.
- Local NLI adds CPU latency (tens of ms per sentence **[VERIFY by measuring]**) and a model dependency. Under CPU contention in load tests it could become its own bottleneck, so it must be timed in the trace.
- V0 cannot catch everything. The V0-only versus V0+V1 gap on wrong answers is a number worth reporting.

### 3.1 Streaming and what "first token" means

Draft is streamed to the user. But verify happens after the draft, so streaming unverified text to the user conflicts with "never send a wrong answer".

| Option | TTFT | Safety |
|---|---|---|
| **A. Buffer the full draft, verify, then release** | Poor (TTFT ≈ total) | Best |
| B. Stream draft immediately, verify after, append correction/retraction | Best | A wrong answer is already shown |
| **C. Stream, but verify with free deterministic Stage A *incrementally* on citations as they close; withhold the final "verified" mark** | Good | Medium |

Decision point to confirm: I recommend **A by default with deterministic verification so cheap it adds ~0 ms, and gate the LLM verify behind budget**. Then TTFT for the user = time the first token is *released*, and I report both "model first token" and "user-visible first token" if they differ. This is itself an interesting measurement for the write-up. The brief says "never wants to send a wrong or embarrassing answer just to stay fast", which backs A.

**Definition to fix now (affects every table):** `start_time` = request arrival at admission. `first_token_ms` = arrival → first token of the final user-visible answer (includes queue wait, limiter wait, earlier stages, retries). `total_ms` = arrival → final byte or terminal failure. Per-call TTFT is logged separately in events.jsonl.

### 3.3 Deadline contract

Each request gets **one absolute deadline** `t_deadline` (monotonic clock) at arrival, stored in its request context. It is **never reset, extended, or replaced** by queue re-entry, retries, regeneration, or reformulation. The only exception is coalescing: a leader's deadline is the latest among its live waiters, and a follower whose own deadline passes detaches without cancelling the leader while others still wait.

Every wait and every call derives its limit from `remaining = t_deadline - now`:

| Point | Rule |
|---|---|
| Door queue | drop at dequeue if `remaining < min_service_estimate` |
| Limiter wait | bounded by `remaining - est_call`; if the bucket will not refill in time, end the request now instead of waiting |
| Retrieval tool | `timeout = min(tool_timeout, remaining - reserve)` |
| Model call | `timeout = min(call_timeout, remaining - reserve)`, plus a separate first-token timeout |
| Retry backoff | sleep only if `remaining` can still fit sleep + one call; otherwise do not retry |
| Regeneration / reformulation | only if `remaining ≥ est_call + V0 time`; otherwise extractive fallback (local) or refuse |
| V0 verification | always runs (local, fast); if the deadline has already passed the answer is not released and the status is `timeout` |

`reserve` holds back time for the mandatory later steps (V0 and release), so an early stage cannot consume the whole deadline.

**Cancellation.** On expiry the request's task is cancelled, the HTTP stream is closed, the slot is released, and the status is `timeout` with the stage it died in. The provider may still count tokens already generated, and I will say so in the write-up. If a load-generator client gives up *earlier* than the server deadline, it drops the connection and the server treats that as cancellation too, so there are two kinds of give-up in the logs (client disconnect and server deadline), both recorded as `timeout`.

Every event in events.jsonl carries `remaining_ms`, which also feeds the heading-4 breakdown.

---

## 4. Rate limiting (the LLM gateway)

### 4.1 What to discover (Table 1), not assume

Expected mechanisms **[VERIFY]**: response headers like `x-ratelimit-limit-requests`, `x-ratelimit-remaining-requests`, `x-ratelimit-limit-tokens`, `x-ratelimit-remaining-tokens`, `x-ratelimit-reset-requests`, `x-ratelimit-reset-tokens`, and `retry-after` on HTTP 429. If true, a cheap probe reads limits straight from headers without ever hitting them. Hitting the limit deliberately is a *second* probe, small and controlled.

Open questions for probing: does TPM count `max_tokens` up front or actual usage? Is the minute window fixed or rolling? Does the daily limit return a different error body from the per-minute one (needed to avoid retrying a daily 429 pointlessly)? Is there a concurrency cap? (Probably not documented; measure by raising concurrency and watching latency/errors, confidence low.)

### 4.2 Client-side limiter design

Goal: **never rely on provider 429s as the control signal.** Use them as a fallback and a measurement.

- **Dual token buckets** (requests and tokens) per model, refilled at the learned rate, with ~10-15% headroom **[tunable]**.
- **Reserve-then-reconcile for tokens:** reserve `est_prompt + max_tokens` before sending; after the response, refund `reserved - actual`. Prevents a few large prompts blowing the TPM.
- **Header-synchronized:** after each response, reset local state from `remaining-*` headers. This corrects drift and notices limit changes ("can change without notice").
- **AIMD on 429:** on 429, multiplicatively cut the permitted rate (and honour `retry-after`); recover additively. Handles an unannounced limit drop.
- **Concurrency semaphore** sized from measurement.
- **Daily budget tracker:** persistent counters (survives restart) with a configurable *artificial* cap. See 4.4.

| Limiter style | Pro | Con |
|---|---|---|
| Fixed-window counter | trivial | boundary bursts violate real limit |
| **Token bucket (chosen)** | smooths bursts, easy to reason about | if provider uses a fixed window, a bucket can still trip it at boundaries; headroom covers this |
| Sliding-window log | exact | more memory, unneeded precision |
| Pure reactive (just retry on 429) | zero tuning | wastes requests, which count against RPD/RPM; inflates latency; the naive baseline I should *compare against* |
| Central distributed limiter (Redis) | needed for multi-instance | pointless for one process; mention as what changes in production |

### 4.3 Scheduling: the simple baseline

**Decision:** one door queue plus a cap on pipelines in flight, instead of per-stage queues. The earlier design (later-stage-first strict priority, EDF within a stage, adaptive LIFO) is kept only as an ablation (see "Ablation" below). Reasons: EDF and newest-first are opposite orderings of the same queue (with equal patience EDF is just FIFO), and stage priority exists only to stop requests stranding between steps, which a cap on in-flight requests also prevents with far less machinery.

**Door (admission) queue**
- Single bounded queue. Order is **FIFO normally**, and **newest-first when the head's wait exceeds a threshold** (head is probably abandoned already, and serving it wastes quota).
- At dequeue, drop any request whose `remaining < min_service_estimate` (a feasibility check, not EDF).
- **In-flight cap N** (a bulkhead): at most N pipelines run concurrently. By Little's law `N ≈ sustainable answers/s × target latency`, where sustainable answers/s comes from the limiter capacity divided by an EWMA of **attempts per answer, including retries and regenerations**. Only N requests exist inside the system, so they cannot strand each other.

**Inside a slot**
- Calls wait on **per-model rate-limit buckets**, with plain FIFO waiters and no skip-ahead. A large call at the head waits for tokens and the calls behind it wait too. This is deliberate. With at most N pipelines the bucket queue is short, and skip-ahead could starve the large call.
- Before each send the gateway checks the deadline (section 3.3).

**Retries and other discretionary calls**

*Discretionary* means any provider attempt that is not a request's first attempt at a step: retries, regeneration after a V0 failure, and the one reformulation loop.

1. **Not a new request.** A discretionary call stays in its request's slot, so concurrent retries are bounded by N, and total work by `N × max attempts`.
2. **No queue-jumping.** It re-enters the bucket FIFO after its backoff, behind first attempts already waiting. There is no priority boost for sunk cost. The cost is that some requests with sunk work time out, and I accept it.
3. **Capped share of quota.** Discretionary calls draw from a sub-bucket holding at most fraction `f` (default ~10-15%, tunable) of RPM and TPM. First attempts always have the remaining share.
4. **Fail fast when the share is empty.** If the sub-bucket cannot serve the call within the remaining deadline, the request ends now (`rate_limited` or `error`), the slot is released, and the next queued request starts. Retries never wait for budget while holding a slot.
5. **Global pause.** A 429 or an open circuit breaker pauses all dispatch (first attempts included) until `retry-after`, then resumes with jitter. Without this, N slots each backing off on their own would resynchronize into a storm.
6. **Feedback into admission.** Because the attempts-per-answer EWMA includes discretionary calls, rising retries shrink N and make the door shed sooner. Retries make admission stricter, never bypass it.
7. **Daily-limit 429s are never retried.** They flip the system straight to ladder level 3/4.
8. **Per-request attempt cap K** (default 5 provider attempts in total), so no request can consume unbounded quota.
9. **Gates apply to discretionary calls too.** Regeneration and reformulation need ladder pressure below a threshold, enough remaining deadline, and budget in the sub-bucket.

**Starvation analysis (baseline)**
- A new request waits at the door only for a free slot. Every slot is freed by completion, by deadline expiry (hard bound), or by fail-fast (rule 4). So slot-hogging by doomed retriers is bounded by the deadline and usually much sooner.
- Old requests lose under newest-first, deliberately, and the deadline turns that into a recorded `timeout`, not an indefinite wait.
- Coalescing: the leader's deadline is the latest among live waiters, and expired followers detach individually (section 3.3).

**Knobs (all in config):** N formula and target latency, newest-first wait threshold, `min_service_estimate`, `f`, K, bucket headroom, pause jitter.

**Invariants asserted by `analyze.py` from events.jsonl** (so the claims are checkable, not just described):
- I1: no provider attempt starts after `t_deadline - min_call_estimate`.
- I2: attempts per request ≤ K.
- I3: discretionary attempts ≤ `f` + small slack of all attempts in any 60 s window.
- I4: concurrent pipelines ≤ N at all times.
- I5: no request is admitted to a slot after its deadline.
- I6: no dispatch happens while the global pause is active.

**Ablation (kept for measurement, not default):** per-stage queues with later-stage-first priority and EDF/LIFO. Compare against the baseline on the stand-in under sustained overload with **goodput** (answers delivered within deadline), **p95 total time**, **wasted calls** (calls from requests that later timed out), **discretionary share of attempts**, and **door wait for new requests**. If the fancy policy does not clearly win, ship the baseline.

**Tradeoffs of the baseline:** N needs tuning (too small leaves quota idle, too large brings back stranding), a slow request occupies a slot until its deadline, and there is no explicit favouring of near-complete requests.

Alternatives considered: pure FIFO with no cap (collapses under overload), weighted fair queuing (overkill, no tenants), user-tier priority (no tiers in the brief), a reservation or ticket model where admission reserves quota for all of a request's calls (most precise, but needs reliable per-request call-count prediction).

### 4.4 Limit provenance, and the daily budget

Every limit value, every limit-related behaviour, and every scenario carries exactly one provenance label. The three are never merged or paraphrased into each other.

| Label | Meaning | Required evidence |
|---|---|---|
| **DOCUMENTED** | stated in Groq's docs, console, or pricing page | URL, date accessed, the quoted value. Never described as "observed". |
| **OBSERVED** | seen in the real service's behaviour during my runs | raw rows in `results/probe_real.jsonl`. Two subtypes: **OBSERVED-REPORTED** (the provider told me in response headers or body) and **OBSERVED-TRIGGERED** (I hit the limit and recorded the response). |
| **SIMULATED** | a value or behaviour set by me, in the stand-in or in a client-side cap | the config file plus the run manifest. A guess about a provider behaviour I never saw is **SIMULATED (guess)**. |

**Rules**
1. **One source of truth:** `config/limits.yaml`. Each entry is `{value, provenance, subtype, evidence, date, confidence}`. No limit value appears in code outside it. If docs and headers disagree, both entries are kept and the disagreement is reported.
2. **Run manifest** `results/runs.json`: for every scenario, the source (real or stand-in), the limit entries it used with their provenance, the config hash, and start/end times. `results.csv` keeps exactly the brief's columns, and `scenario` joins to the manifest.
3. **`analyze.py` refuses to emit a table row** for a scenario with no provenance entry, and prints a provenance footer under each table.
4. **Table 1:** the "How you found it" cell starts with the label. Anything never seen live is written **"not observed"** next to its documented value, or "not measured" if not even documented.

**What the daily limit can honestly be, by default**

| Item | Expected label | Note |
|---|---|---|
| RPD / TPD value | DOCUMENTED, plus OBSERVED-REPORTED only if headers carry it **[VERIFY]** | I will not claim a measured daily limit I have not seen reported |
| Error code/body/headers when the daily limit is hit | **not observed**: DOCUMENTED if the docs specify it, otherwise SIMULATED (guess) | I do not plan to exhaust the real daily budget, which would block the rest of the work |
| Time until daily recovery | DOCUMENTED **[VERIFY]** | not observed |
| Per-minute limit hit: response, headers, retry-after, recovery | OBSERVED-TRIGGERED (one small controlled probe) | the only limit I plan to trigger live |

**Scenario labelling**
- `daily_exhausted`: source = stand-in, limit provenance = **SIMULATED**. The stand-in's daily-limit response is never described as "what Groq does".
- `daily_exhausted_simcap` (optional): source = real, with the gateway's `daily_budget_override` as a small artificial cap. The calls are real, the exhaustion event is **SIMULATED**, and the provider never returned a daily 429. The scenario name and manifest say so, because `source = real` alone would mislead.
- If I do hit a real daily limit (by decision or accident), that behaviour becomes OBSERVED-TRIGGERED, recorded as new evidence, and the stand-in is re-calibrated and relabelled.
- Table 2a/2b "Real or stand-in" cell for the daily row reads "stand-in (limit SIMULATED)".

**A risk this creates:** the gateway must tell a daily 429 from a per-minute 429 so it never retries the daily one. If the daily shape was never observed, classification can only use documented fields or a heuristic (for example, a `retry-after` or reset value far beyond a minute means non-retryable). That heuristic is an assumption, labelled **SIMULATED (guess)**, logged with every classification it makes, and a strong candidate for heading 8's "least certain assumption": if it is wrong, a daily 429 gets retried and wastes quota, or a long per-minute reset gets mistaken for daily and requests are dropped needlessly. The fail-safe direction is to treat an ambiguous 429 with a long reset as non-retryable.

**Cost of this discipline:** a simulated daily-exhaustion result is weaker evidence than an observed one, and the bookkeeping (limits.yaml, manifest, footers) adds work. I accept that, because a reviewer who finds a simulated number presented as measured will distrust the rest.

---

## 5. Load management

### 5.1 Admission control

Accept if `estimated_wait + estimated_service < deadline`, where estimates come from current queue depth and the gateway's measured service rate. Otherwise shed immediately with an honest "busy" reply (cheaper and kinder than letting the user wait 30 s then fail). Bounded queue (hard cap) as a backstop. Admission works together with the in-flight cap N in section 4.3, and its service-time estimate includes retries and regenerations (attempts per answer), so retry pressure tightens admission instead of bypassing it.

Alternatives: unbounded queue (collapses; this is the baseline to demonstrate), token-bucket admission at the door only (ignores per-request cost), CoDel-style queue-delay-based dropping (good; admission-by-estimate plus dequeue-time deadline check approximates it).

### 5.2 Coalescing and caching, probably the biggest win

The brief says bursts happen "right after something goes wrong for many users at once". **Many users ask the same question.** Single-flight coalescing means 200 identical questions cost 1 pipeline run. This is the cheapest "nobody turned away" lever. Cache key = normalized question + retrieval setting.
Caveats to state: it inflates apparent throughput, so the load generator must be run **with realistic question repetition and also with unique questions**, and reported separately, or the numbers mislead. Cached answers can go stale (not an issue with a fixed library). Risk of caching a bad answer: only cache verified answers.

### 5.3 Degradation ladder (the thesis)

Driven by gateway pressure = max over (RPM used %, TPM used %, daily used %, queue wait / deadline).

| Level | Pressure | Behaviour | What is given up |
|---|---|---|---|
| 0 Full | low | LLM rewrite if ambiguous, k chunks, draft, LLM verify | nothing |
| 1 | moderate | skip LLM rewrite; skip **V1** (the optional LLM verify). **V0 still runs in full.** | some semantic robustness, not basic correctness |
| 2 | high | reduce k (fewer prompt tokens), smaller model, lower `max_tokens` | answer depth/quality |
| 3 | very high | **extractive fallback:** no LLM; return the top cited passages verbatim with a note | synthesis; still cited and not fabricated |
| 4 | exhausted | cache hit or honest "busy, try at HH:MM" | the answer |

**Invariants that never degrade:** the full V0 verification tier (section 3.2) runs on every released answer, citations are required, unsupported claims are never released (regenerate, extractive fallback, or refuse), and no fabricated claims. The extractive fallback is safe by construction (verbatim text), but it must still pass the relevance threshold, or it would show irrelevant passages for an unanswerable question, which is embarrassing.

### 5.4 Retry policy (heading 6)

- Retry only: 429 (per-minute), 5xx, connect errors, timeouts. **Never** retry a daily-limit 429, 4xx validation errors, or content failures.
- Exponential backoff with **full jitter**, capped; honour `retry-after` as a floor.
- **Per-request cap** (max 2 retries) and a **global retry budget** (retries ≤ ~10% of recent traffic, token-bucketed). This stops retry storms.
- **Deadline-aware:** the request's original deadline is never extended (section 3.3). Never retry if the remaining time cannot fit backoff plus another attempt.
- Retries are *discretionary calls* governed by section 4.3: they stay in the original slot, never jump the bucket queue, draw from a capped sub-bucket (fail fast when it is empty), respect the global pause, and feed back into admission.
- **Circuit breaker** on repeated failures: stop sending, probe occasionally.
- **No hedging** (duplicate request after a delay). It cuts tail latency but doubles quota use, which is wrong when quota is the scarce resource. Worth a sentence in the write-up as a rejected alternative.

**Timeout design matters:** "some calls succeed slowly instead of failing quickly." A timeout shorter than the slow-success tail causes retries of calls that would have succeeded, doubling load: the planned **"retry made things worse" demonstration** (naive 2 s timeout + 3 retries vs budgeted policy, same stand-in, same overload). This produces the exact case heading 6 asks for, with numbers.

---

## 6. Retrieval

### 6.1 Ingestion

- Fetch `https://arxiv.org/pdf/<id>v<N>` at the pinned versions; save PDFs with SHA-256 in a manifest so the exact artifact is provable. Also record the version shown in the PDF itself, since arXiv serves what you ask for but I should confirm the text matches that version (e.g. check the date/header). **[VERIFY]**
- Extract per page with PyMuPDF, keep `paper_id, page, section heading` metadata. Detect section headings by font size/numbering heuristics; fall back to page-only citation if detection is poor.
- Clean: strip headers/footers, de-hyphenate line breaks, drop the references section from the index (or index it but tagged so it is never cited as evidence).

### 6.2 Chunking settings to compare (Table 3)

| ID | Piece size | Overlap | k fetched | Rerank | Hypothesis |
|---|---|---|---|---|---|
| S1 | ~256 tok fixed | 32 | 3 | no | cheapest, precise but loses context |
| S2 | ~512 tok, section-aware | 64 | 5 | no | likely best quality/cost balance |
| S3 | ~512 tok, section-aware | 64 | 8 → rerank → 4 | cross-encoder | best quality, more latency |
| S4 | ~1024 tok | 0 | 3 | no | big context, token-expensive, diluted |

Each run on the same ≥20 questions. Report correct+cited %, wrong %, correctly refused %, tokens per answer, TTFT. Expected pattern, to be confirmed or refuted by data: S1 misses multi-sentence answers, S4 spends tokens and dilutes, S3 wins on quality but is not free. Include a "when the limit is close" rule: **drop to the cheaper setting dynamically** (S3 → S2 → S1) and as a last step go extractive, as in the ladder. Token cost of retrieval is concrete: prompt tokens ≈ k × chunk size + instructions, and that feeds TPM directly.

### 6.3 Citation format

`[paper-short-name §section, p.N]` plus chunk id internally. Verification checks that the cited chunk id was actually retrieved and the quoted phrase appears in it. Granularity tradeoff: page-level is robust to heading-detection errors, section-level is more useful to a human. Cite both when available.

### 6.4 How this library will mislead a naive retriever (heading, three ways, to be validated against the real text)

1. **Shared vocabulary across papers.** "attention", "transformer", "encoder/decoder", "hierarchical", "latency" appear in all three. Question *"How many layers does the model use?"* with no paper named can return a confident chunk from the wrong paper. Mitigation: paper filter from detected names, and refuse or ask when ambiguous.
2. **Numbers live in tables, and PDF extraction scrambles tables.** Questions like *"What BLEU score did the big model get on English-German?"* retrieve the prose around a table, not the numbers, or retrieve a mangled row with columns misaligned. Mitigation: table-aware extraction or manual table chunks; test it explicitly.
3. **Abstract/intro/conclusion repetition and "answerable-looking" unanswerable questions.** The same claim appears 3 times, so top-k fills with near-duplicates and crowds out the one chunk holding the detail (needs dedup/MMR). Separately, *"How does this compare to GPT-4?"* or *"What dataset did Moshi's authors use for X (not in paper)?"* produce high-similarity but irrelevant chunks, so a retriever with no abstain threshold hands the LLM bait to hallucinate from.
   Other candidates if replacements are needed: version drift (v7 text differs from commonly quoted versions, so answers "from memory" can be wrong), reference-list pollution, symbol-heavy math chunks that embed badly.

I have not read the papers. These are predictions; confirm each with a real failing query before writing them up.

---

## 7. Stand-in service

OpenAI-compatible `/v1/chat/completions`, SSE streaming, plus `/control` to change behaviour at runtime and `/stats` for server-side truth.

| Behaviour | Mechanism | Calibrated from |
|---|---|---|
| **Slow** | TTFT ~ lognormal(μ,σ) | fit to measured real TTFT distribution |
| **Streaming** | emit tokens at rate r with jitter, in chunks | measured real tokens/sec |
| **Load-dependent slowdown** | latency × (1 + α · inflight) beyond a knee | real runs at concurrency 1, 2, 4, 8 (slope low-confidence) |
| **Refuse (limit)** | own RPM/TPM/RPD counters, returns 429 with the same headers/body/retry-after shape as real | per-minute shape: OBSERVED-TRIGGERED. Daily shape: DOCUMENTED if specified, otherwise SIMULATED (guess). Labels per section 4.4 |
| **Fail** | p(5xx), p(connection reset), p(hang without response) | rough; real failure rate may be ~0, so this is a **chosen** parameter, not a measurement, and must be labeled as such |
| **Slow-succeed** | small probability of a very long but eventually successful call | tail of real latency, else chosen |
| **Mid-stream failure** | drop connection after n tokens | chosen |
| **Content** | templated reply that quotes the first retrieved chunk with a valid citation (parsing the prompt) | n/a |

**Where it is least like the real thing (to state in heading 2):**
1. Real Groq is extremely fast and its tail is dominated by provider-side queueing I cannot observe; mine is a parametric curve.
2. Real failures are correlated (provider incidents last minutes); mine are mostly independent coin flips unless I script outages.
3. Output *content* is fake, so **no quality number may ever be taken from the stand-in.** Quality is measured on real runs only. This is a hard rule.
4. Token counting and prompt-size-dependent latency are approximated.
5. Real limit behaviour may change without notice; the stand-in's is frozen at what I observed.
6. Its daily-limit behaviour is SIMULATED, not observed (section 4.4). Heading 2 will say so in one explicit sentence.

---

## 8. Experimentation and observation

### 8.1 Phase A: probe the real service (modest, planned quota)

Budget it before running. Illustrative plan **[sizes tunable after reading limits]**:
1. Read docs for limits; send 1 call and inspect all headers (gives limits for ~free).
2. ~40 sequential calls, varied prompt/output length → latency distribution, TTFT vs token count, decode rate.
3. Concurrency ladder 1/2/4/8, ~15 calls each → slowdown vs concurrency.
4. One *controlled* per-minute limit hit (short burst just over RPM) → 429 body, headers, `retry-after`, recovery time. Do this **last** and once.
5. No deliberate daily exhaustion unless RPD is small (decision gate).

**Ask me before any of this runs.** Then I give you the commands to execute.

### 8.2 Phase B: scenarios (Table 2a/2b)

| Scenario | Target | Shape | Source |
|---|---|---|---|
| Steady | below limit | constant Poisson, e.g. 70% of capacity | real (small) + stand-in |
| Burst | far above limit briefly | 10× rate for ~20 s, then idle, then recover | stand-in; real only if tiny |
| Sustained overload | 2-3× capacity for minutes | constant | stand-in |
| Per-minute limit halved | | steady load while limiter/stand-in limit is halved at t=T | real client config + stand-in |
| Daily budget exhausted | | budget hits zero mid-run | stand-in, limit **SIMULATED** (optional `_simcap` variant on real calls, exhaustion still SIMULATED; see 4.4) |
| Retry-storm (extra) | | naive vs budgeted retry policy | stand-in |
| Duplicates vs unique (extra) | | same load, high repetition vs all-unique | stand-in |
| Scheduler ablation (extra) | | sustained overload, baseline (door queue + cap N) vs per-stage priority/EDF/LIFO | stand-in |

**Open-loop generation is mandatory.** Closed-loop generators (wait for response before the next send) slow themselves down when the system slows down and hide the overload, the "coordinated omission" error. Arrivals follow a schedule independent of responses. Each simulated user has a **patience** (deadline); when it expires the request is recorded as given up (`timeout`).

### 8.3 Observation: what gets logged

`events.jsonl`, one line per event with `request_id, stage, event, t_mono`:
`arrive, admit/shed, queue_enter(stage), queue_exit, limiter_wait_start/end, send, first_byte, first_token, last_token, 429(+retry_after), retry_sleep_start/end, tool_start/end, verify_result, done(status)`.
From this derive the heading-4 split for any slow request:
- **waiting** = admission queue + limiter wait + stage queue
- **working** = sum of actual call durations (TTFT + decode) + tool + local compute
- **retrying** = backoff sleeps + the wasted duration of failed attempts
- and whether the largest contributor sat **before or after the first user-visible token**, which falls out of timestamp ordering.

`results.csv` is generated from events (not hand-built) with exactly the brief's columns. Decisions to lock:
- `status` allows only ok / rate_limited / timeout / error. Mapping: client deadline passed or shed at admission → `timeout`; final failure from 429s after retries → `rate_limited`; 5xx/tool/network → `error`. The finer reason is kept in events.jsonl and I will say so in the write-up. (The brief's enum has no "shed" value. This is my interpretation and I should flag it.)
- `tokens_in/out` = summed over all calls for the request, taken from provider `usage` (stand-in emulates it).
- `retries` = count of retried calls across the request.
- `retrieval_setting` = S1-S4 id; `answer_grade` empty for non-quality runs, filled for quality runs. Grade for stand-in rows is `not graded` or blank (never invented).
- **Cost per 1,000 answers:** free tier costs $0, so report *notional* cost from Groq's published per-token pricing for the model used **[VERIFY pricing page]**, clearly labeled notional.

### 8.4 Quality evaluation

- ≥ 20 handwritten questions (brief minimum 15), mix: ~8 single-fact answerable, ~4 multi-hop or cross-paper, ~3 numeric/table, ~5 **unanswerable** (plausible but not in papers). Each has: gold answer facts, expected paper/section, and for unanswerable a label.
- **Grading:** deterministic first (required facts present, citation resolves to the expected paper/page, refusal phrase for unanswerable) then **manual review of every answer** for 20-60 rows. An LLM judge costs quota and adds a second source of error; rejected, mention as the scalable alternative.
- Grades: correct (right + cited), wrong, refused_correctly, refused_wrongly.
- Run each question across S1-S4 on the real service. Quota math: 20 q × 4 settings × ≤3 calls ≈ 240 calls, plus reruns. Fine, but it shares RPD with probing, so total the budget first.

---

## 9. Cross-cutting tradeoffs (summary)

| Tension | My lean | Costs |
|---|---|---|
| Speed vs correctness | buffer + verify before release | TTFT worse than streaming raw |
| Calls per answer vs robustness | mandatory local V0 checks always; LLM calls (rewrite, V1) optional | weaker semantic verification at high load; V0 false rejections can trigger extra regenerations under load |
| Fairness vs served-count | adaptive LIFO under overload | oldest requests lose |
| Retry for success vs protect quota | budgeted retries, deadline-aware, no daily retry | some transient failures surface to user |
| Nobody turned away vs wrong answer | extractive fallback or honest busy | degraded usefulness, never a fabrication |
| Real realism vs controllability | real for quality + small probes, stand-in for load | load numbers depend on stand-in fidelity |
| Cache/coalesce vs honest benchmark | report with and without repetition | benchmark complexity |

---

## 10. Implementation plan

Project layout:
```
relay/
  config/ (relay.yaml, scenarios.yaml)   .env (git-ignored)   .gitignore
  data/ (papers/, manifest.json, chunks/, index/, questions.yaml)
  src/relay/
    ingest.py  chunk.py  index.py  retrieve.py
    gateway.py (limiter, queues, retries, breaker, budget)
    pipeline.py (state machine)  prompts.py  verify.py
    admission.py  cache.py  tracing.py  sink.py
  standin/server.py  standin/profile.json
  tools/probe.py  tools/loadgen.py  tools/analyze.py  tools/fit_profile.py
  results/ (probe_real.jsonl, results.csv, events.jsonl)
  REPORT.md  CONVERSATION.md
```

| # | Phase | Deliverable | Gate before next | Est. |
|---|---|---|---|---|
| 0 | Setup | repo, .gitignore, key handling, config skeleton, decisions locked (Python, model, definitions in section 3.1) | you confirm choices | 20 min |
| 1 | Corpus + retrieval | pinned PDFs + manifest, chunks with page/section, BM25+dense, retrieve CLI, **questions.yaml with gold labels** | retrieval returns sensible hits on 5 hand-checked queries | 75 min |
| 2 | Stand-in v0 | OpenAI-compatible SSE server with fixed params, control endpoint | pipeline can run end-to-end offline | 45 min |
| 3 | Gateway + pipeline | limiter, queues, retries, tracing, state machine, results.csv sink | full pipeline runs on stand-in, CSV schema valid | 120 min |
| 4 | **Probe real Groq** (needs your go-ahead; you run the commands) | probe_real.jsonl, filled Table 1 | you review raw output | 40 min |
| 5 | Calibrate stand-in | `fit_profile.py` → profile.json; sanity-compare real vs stand-in distributions | stand-in distribution plausibly matches | 30 min |
| 6 | Load scenarios | loadgen, 5 required + retry-storm + duplicates, runs saved | each scenario's numbers recomputable | 60 min |
| 7 | Quality + retrieval study | S1-S4 on real service, manual grading | Table 3 filled | 60 min |
| 8 | Analysis + write-up | `analyze.py` generates all tables; REPORT.md within 2 pages; conversation history assembled | numbers match CSV | 90 min |

About 9 hours with slack; the brief says 6-8, so cut candidates are: S4, the duplicates scenario, and LLM verify.

**Principles during build:** build the stand-in and the gateway before touching the real API; every number in the report comes from `analyze.py`, never by hand; commit after each phase; keep the unedited conversation transcript.

---

## 11. Open decisions for you

1. **Language:** Python (my recommendation) or Go?
2. **May I read Groq's rate-limit and pricing pages (web fetch/search)?** Docs are public, no key involved, but you asked me to ask before third-party requests.
3. **Verbatim vs buffered streaming** (section 3.1): confirm "buffer, verify, release" as default.
4. **Quota plan:** once you tell me your plan's limits, I fix the probe sizes.
5. **Vaani feedback and the "distinctly yours" content must be yours.** I can structure them, but the opinions, the retained prompts, and the "what I caught / overruled" paragraph have to come from what actually happens. Tell me where I got something wrong along the way so it is on record.
6. **Conversation history:** this session's transcript is part of the deliverable. Decide how you export it (Claude Code stores transcripts locally; I can tell you where when we get there).

---

## 12. Decisions locked and documented limits (2026-11-12)

**Decisions:** Python. Read Groq's public docs only, consuming no quota. Buffer, verify, release is the default. Documented quota limits are discovered before any live probing. Still true: no code is run and no third-party API call is made without explicit permission.

### 12.1 What the docs say (label: DOCUMENTED; full detail in `config/limits.yaml`)

Read by page-fetch agents, so extraction was model-summarised: confidence **medium** until you cross-check against the pages and your own console Limits page.

| Item | Documented value |
|---|---|
| Limit types | RPM, RPD, TPM, TPD (plus audio and ITPM/OTPM); applied per organization; whichever is reached first binds |
| Candidate table row, `openai/gpt-oss-20b` and `-120b` | 30 RPM, 1K RPD, 8K TPM, 200K TPD (**plan label unresolved**, see below) |
| Cached tokens | do not count toward limits |
| Headers on every response | `x-ratelimit-limit/remaining/reset-requests` (**RPD**), `x-ratelimit-limit/remaining/reset-tokens` (**TPM**); `retry-after` (seconds) on 429 only |
| 429 body | `{"error": {"message", "type"}}`; no `code` field documented |
| Minute vs day 429 distinguishable? | **not stated on page** |
| Max_tokens counted up front for TPM? | **not stated on page** |
| Fixed vs rolling window | **not stated on page** |
| Concurrency limit | **not stated on page** |
| Streaming | `stream` param, SSE, ends with `data: [DONE]`; where usage appears in a stream: **not stated on page** |
| Notional price (`gpt-oss-20b`) | $0.075 in / $0.30 out per 1M tokens (`gpt-oss-120b`: $0.15 / $0.60). Llama 8B/70B price: not a number on the page |

**Resolved by your console paste (section 12.5). Original note on which plan the table describes:** The page has Free and Developer tabs, only one table was extracted, and it was not labelled. The values look free-tier, yet the page text says "Developer plan", and the models page gives a different TPM for the same model. **Action for you (costs no quota):** open your console's Limits page (`console.groq.com/settings/limits`) and send me the numbers for the models we might use.

### 12.2 Design consequences (conditional on the 8K TPM / 1K RPD / 200K TPD candidate row being right)

1. **Tokens, not calls, are the likely binding limit.** A draft call with five 512-token chunks plus instructions is roughly 3-4K prompt tokens, so 8K TPM allows only about 2 such calls per minute, even though 30 RPM would allow 30. Sustainable answers/min ≈ `min(RPM / calls_per_answer, TPM / tokens_per_answer)`. This reorders my earlier framing ("calls per answer is the currency"): prompt tokens per answer are probably the currency, which makes retrieval size (k × chunk size) the main throughput lever and may rule out settings like S4 (3 × 1024) on the real service.
2. **The experiment budget is tiny.** 200K TPD at ~3.5K tokens per call is on the order of **60 calls per day**. The quality study (20 questions × several settings) and the probes cannot all fit in one day on the real service. Quota planning becomes a first-class task, and the work may span several days (the brief allows 4).
3. **Reasoning models change token counts.** The candidate models are `gpt-oss` models; whether hidden reasoning tokens count as completion tokens against limits is **not stated on page** and must be measured.
4. **Header sync is partial.** Headers give RPD and TPM only. RPM and TPD cannot be OBSERVED-REPORTED, only DOCUMENTED or triggered. The limiter's RPD tracking can sync from headers (good for the daily budget), but its RPM bucket runs on the documented value.
5. **Daily-vs-minute 429 classifier** (section 4.4) now has a better input than `retry-after` alone: on a 429, `x-ratelimit-remaining-requests == 0` suggests the daily limit and a low `remaining-tokens` suggests the minute limit. Still a heuristic until observed, so still SIMULATED (guess) in the logs.
6. **Prompt caching:** cached tokens do not count toward limits, so a fixed instruction prefix could save real quota. Whether caching applies to our model is not documented in what I read. Worth one measured check.
7. **Model choice** is now bounded: `openai/gpt-oss-20b` (cheapest documented price, same candidate limits as 120b, fastest listed) is the leading candidate for all calls. A second, larger model adds a second set of buckets for little gain at 8K TPM. Llama 8B/70B limits were not in the extracted table, so they cannot be planned yet.
8. **Cost per 1,000 answers is notional** and computed from the documented gpt-oss-20b price, labelled as such. A free tier costs nothing, so the figure is "what this would cost at the on-demand price".

### 12.3 Gaps the docs do not close (must be measured or marked "not observed")

Whether `max_tokens` is reserved up front, window type, concurrency cap, per-minute 429 body and `retry-after`, whether failed calls count against RPD, streamed usage location, timing units, reasoning-token accounting, and whether the daily 429 differs from the minute one. Each stays "not stated on page" in Table 1 until a probe settles it, and the probe plan must be sized by the real TPD (section 12.2 point 2).

### 12.4 Next steps

1. You send the real limits from your console Limits page (no quota used).
2. I revise section 8.1's probe sizes from those numbers and write a token-budget table for the whole project.
3. Then Phase 0 and Phase 1 (setup, then corpus and retrieval) begin. Those need no quota at all.

### 12.5 Account limits confirmed (supersedes 12.4 step 1)

Your console numbers match the public table, so the plan question is closed. Snapshot: `config/account_limits_2026-11-12.md`. Label stays **DOCUMENTED**, not observed. Models available to plan with: `openai/gpt-oss-20b`, `openai/gpt-oss-120b`, `qwen/qwen3.8-27b` (each 30 RPM / 1K RPD / 8K TPM / 200K TPD), and `allam-2-7b` (30 RPM / 7K RPD / 6K TPM / **500K TPD**). Llama 8B/70B are not on your account.

**What the numbers force**
1. **Tokens bind, requests never do.** TPM/RPM = 8000/30 ≈ **267 tokens per call**; any call bigger than that hits TPM before RPM. Likewise TPD/RPD = 200 tokens, so **RPD (1K) is irrelevant**; the daily limit that matters is TPD (200K). Every realistic call here is over 1K tokens.
2. **Throughput on gpt-oss-20b:** a ~3.5K-token draft call allows ~2 calls/min and ~57 calls/day. A ~1.5K-token call (S1-sized: 3 × 256-token chunks + instructions + short answer) allows ~5/min and ~130/day. **Prompt size is the main lever on both rate and daily capacity.**
3. **`allam-2-7b` has 2.5× the daily token budget but a lower TPM (6K).** It is listed with no further detail; I believe it is an Arabic/English model, which may or may not answer English technical questions well. **Not verified.** It could not be used for quality numbers without a quality check, but it might serve as a cheaper rung in the degradation ladder.
4. **Reasoning-token cost is the biggest unknown.** If `gpt-oss` hidden reasoning counts as completion tokens, a "short" answer could cost several hundred extra tokens. Whether a low-reasoning setting exists on this API is not in the pages I read **[VERIFY]**. The first probe should measure `completion_tokens` against visible output length.
5. **Limits are per model row.** Using more than one model is within the documented table and fits the brief's "limits differ by plan and by model", but spreading load across models to multiply quota could look like a workaround. If used, it is a deliberate, disclosed degradation-ladder rung (smaller model), not a quota trick, and the write-up says so. Default plan: one primary model.
6. **The daily token limit may be hit naturally.** At ~200K tokens it is reachable in one day of real work. That could make the daily-limit behaviour **OBSERVED-TRIGGERED for free**, if every 429 body and header is captured raw. Option for your decision (not done without your say-so): after a day's planned work, deliberately spend leftover quota once to record what the daily limit returns. It costs nothing extra, because that day's budget is otherwise unused, and it would replace a SIMULATED (guess) with an observation. The cost is that real work is blocked until reset, and the reset time is itself undocumented (the first response's `x-ratelimit-reset-requests` header gives the RPD reset, which can be OBSERVED-REPORTED).
7. **Refusals can be free.** Unanswerable questions that the deterministic decide step refuses never call the model, so they cost 0 tokens. That helps the quota plan and is also a quality risk to measure (false refusals).

**Illustrative quota plan on one model (DOCUMENTED limit of 200K TPD per day; numbers are estimates, to be revised after the first measured token counts)**

| Day | Activity | Rough tokens |
|---|---|---|
| A | Probes: headers, latency spread, concurrency ladder, one per-minute limit hit; measure reasoning-token overhead | 30-50K |
| B | Quality study, S1 and S2 on 20 questions (about 15 of them reach the model) | ~100-150K |
| C | Quality study, S3 and S4 (S4 on a subset if too costly), plus small real-service runs of steady load | ~150-200K |
| D | Final real reruns if needed; optional deliberate daily-limit observation | remainder |

The stand-in absorbs all heavy load runs. Total real tokens needed is more than one day's quota, so **how many days are left before submission matters**: it decides how many retrieval settings and questions can be graded on the real service.

**Open questions for you**
1. How many days remain before the 4-day deadline?
2. Is a deliberate end-of-day daily-limit observation acceptable to you (point 6), or do you prefer to keep that scenario SIMULATED?
3. May I plan on `openai/gpt-oss-20b` as the single primary model?

**Answers (2026-11-12):** 1 day remains. Do not deliberately exhaust the limit (the daily-limit scenario stays SIMULATED, section 4.4). `openai/gpt-oss-20b` is the single primary model, with a model registry in config for future models (no alternate provider implemented).

---

## 13. One-day plan and decisions (2026-11-12)

**Priority order:** core pipeline, experiments and `results.csv` first; dashboard afterwards; everything else deferred.

**Scope cuts (to fit one day and ~200K tokens, keep at least 20% in reserve):**
- No LLM query rewrite, no V1 (optional LLM verify), no cross-encoder rerank, no scheduler ablation, no duplicates-vs-unique scenario.
- V0 check 5 (unsupported-claim check) is lexical and number/entity grounding, not a local NLI model (avoids a PyTorch install).
- "Rerank" is a cheap local MMR dedup.
- Real-service generation runs on three retrieval settings; all four are scored retrieval-only (free).
- Dark mode, retrieval analytics view, compare view and timeseries extras are deferred.

**Phase 0/1 implementation deviations from earlier sections** (the code is the source of truth where they differ):
1. **No separate index files.** Chunks are persisted as `data/processed/chunks_<setting>.jsonl`. BM25 is built in memory at load (own implementation in `bm25.py`; expected well under a second for three papers). Supersedes the `data/index/*.pkl` in section 10 and the `build-index` command.
2. **Per-run artifact directories** `results/runs/<run_id>/{manifest.json, events.jsonl, results.csv}` instead of a single sink. `analyze.py` merges them into the submission `results.csv`. Supersedes the single-file layout in section 8.3 and the `results/` tree in section 10.
3. **Token counts for chunking are approximate** (characters ÷ 4). Real token counts always come from the provider's `usage` field and are never replaced by this approximation in any results table.
4. **Chunking is word-window based** with a size and overlap in approximate tokens, optional section boundaries, a small-segment merge rule and a tail-absorption rule (documented in `chunk.py`). The references section is excluded from the index.
5. **Citations use PDF page numbers** (physical page index, 1-based), not printed page labels.
6. **Abstain rule:** the top hit's query-term coverage (fraction of distinct query terms present) must reach `abstain_min_coverage`, or the retriever abstains. The threshold is a starting value to calibrate with the retrieval eval, not a measured result.
7. **Evaluation questions are written by the user**, per the brief. Gold labels are page and/or verbatim evidence phrases. Nothing in `data/` pre-fills them.
8. **Python 3.9 is the minimum** (the user's interpreter is 3.9.6, not the 3.12 assumed in section 1). Rules for all later code: keep `from __future__ import annotations`, never evaluate `X | Y` types at runtime, and avoid 3.10+ features (`match`, `zip(strict=)`, `asyncio.timeout`, `asyncio.TaskGroup`; use `asyncio.wait_for` and `gather`).

## 14. Dashboard (approved, built after the core pipeline)

**Stages, in order:** Overview (live, SSE), Experiments, Traces, Limits. Deferred: Retrieval view, compare view, dark mode. The dashboard is **read-only** and driven only by logged data.

**Architecture.** Four independent processes that share only files: the experiment runner/CLI (writes artifacts), the stand-in server, the dashboard API (FastAPI, GET-only, `127.0.0.1`, tails `events.jsonl` to SSE), and the React + TypeScript + Tailwind + Recharts UI (static bundle).
- The runner never imports the dashboard. The dashboard imports only `relay.artifacts` (schema) and `relay.metrics` (pure functions). `relay.metrics` is also what `analyze.py` uses, so the dashboard and the report tables cannot disagree.
- The dashboard API has no provider access: no `.env`, no `httpx`, no Groq calls, so it cannot spend quota.

**Required runner events beyond section 8.3:** `gateway_state` sampled once a second (queue length, in-flight, bucket levels, pause flag; needed for Table 2b peak queue length), `ratelimit_headers`, `usage`, `retrieval` (setting, hit ids, scores, abstain flag).

**Never fabricate (enforced):**
1. Every API number carries `value`, `n`, `provenance`, `source`, `computed_from`, `as_of`; missing data is `null` with a reason, never 0.
2. No mock data in the shipped bundle and no demo mode; only tests use fixtures.
3. A percentile with n < 20 is flagged, and p99 with n < 100 reads "too few for p99".
4. Provenance is shown by pattern, not colour alone: solid = observed, outlined = documented, hatched = simulated or stand-in.
5. The trace split shows an explicit "unaccounted" remainder and never forces a sum.
6. Provider limits show three layers: documented values, last response headers with an as-of time, and tokens counted from our own logs. RPM and TPD read "not reported by headers". The dashboard never polls Groq.

**API (all GET):** `/api/health`, `/api/runs`, `/api/runs/{id}`, `/api/runs/{id}/summary`, `/api/runs/{id}/requests`, `/api/runs/{id}/requests/{rid}/trace`, `/api/runs/{id}/timeseries`, `/api/runs/{id}/distribution`, `/api/runs/{id}/invariants`, `/api/limits`, `/api/limits/ledger`, and `/api/stream?run_id=` (SSE: `hello`, `run_started`, `request_event` batched every 250 ms, `summary_tick` every second, `run_finished`, `stale`, `data_error`, `heartbeat` every 15 s). SSE ids are byte offsets in `events.jsonl` so `Last-Event-ID` resumes without gaps or duplicates; the tailer parses only complete lines and reports a malformed line as `data_error` instead of dropping it.

**Design direction (frontend-design skill).**
- Palette: Chalk `#E8EDF1`, Paper `#F8FAFB`, Ink `#1B2530`, Slate `#7F93A8` (waiting), Teal `#0E6F6A` (working), Signal `#D4491F` (retrying, failure; a darker variant for text).
- Type: IBM Plex Sans (variable, tabular figures) for UI and numbers; Newsreader for plain-language captions. Self-hosted, no CDN.
- Layout: a left rail with the run list and view names, a status sentence on top, and ruled sections instead of a card grid. Left-aligned, numbers right-aligned.
- The memorable element is the **request track**: each request is a lane of waiting, working and retrying segments with a first-token marker and a cross where the user gave up. It is custom SVG; Recharts handles distributions, time series and outcome bars. Motion is limited to live lanes extending, and is disabled under reduced motion.

**Dependencies.** Backend adds `fastapi`, `uvicorn`, `pytest-asyncio` (dev). Frontend: React 18, TypeScript 5, Vite, Tailwind 3.4, Recharts, TanStack Query, two self-hosted font packages, vitest, testing-library, ESLint, `openapi-typescript`; a tiny hash router instead of react-router; Node 20+. No UI kit, no state library, no runtime network calls except localhost. Versions pinned exactly at install.

**Acceptance criteria (dashboard).**
- *Integrity:* percentiles match hand-computed references; dashboard summary equals the `analyze.py` Table 2 row to one decimal; every API metric has `n`/`provenance`/`source`/`computed_from` (test fails otherwise); empty results directory gives 200 with nulls and reasons, no zeros; no hard-coded metric arrays outside tests; provenance renders text plus a distinct pattern for all three states.
- *Independence and safety:* import-graph test (runner never imports dashboard; dashboard imports no `httpx` and never reads `GROQ_API_KEY` or `.env`); API starts without `.env`, binds `127.0.0.1`, rejects traversal in `run_id`, exposes no non-GET route; killing the dashboard mid-run does not affect runner output.
- *Live:* appended events reach the stream within 1 s; reconnect resumes cleanly; no events for 15 s during a running run shows "No events for N s"; UI stays at or under 4 updates/s on a synthetic 20K-line test file.
- *Frontend quality:* `typecheck`, `build`, `test` pass; regenerated API types give no diff; works at 360px and 1440px with no horizontal page scroll; every chart has a view-as-table toggle; visible focus; AA contrast; reduced motion honoured; Network tab shows only localhost; trace view shows the before/after-first-token marker and the three-way split with an explicit "unaccounted" remainder; Limits view shows the three layers with as-of times.

Charts are written only after loading the `dataviz` skill; the UI is written with the `frontend-design` skill.

## 15. Phase 2 as built (2026-11-13): stand-in, gateway, pipeline, runner

Code is the source of truth where it differs from sections 3 to 8. Nothing here has been run by the assistant; the author runs the tests and scenarios.

**Files.** `standin/{profile,server}.py`, `llm_client.py`, `gateway.py`, `admission.py`, `tracing.py`, `artifacts.py`, `prompts.py`, `verify.py`, `pipeline.py`, `metrics.py`, `runner.py`, `config/scenarios.yaml`, CLI commands `standin`, `list-scenarios`, `run`, `summarize`. New dependencies: `fastapi`, `uvicorn`.

**Kept as designed.** One absolute deadline per request, never extended. Bounded door queue (FIFO, newest-first once the head is stale) plus an in-flight cap N, with admission by estimate and a dequeue-time feasibility check. Retries are discretionary calls: they stay in the slot, re-enter the same FIFO behind first attempts, draw on a capped share (fail fast when empty), respect a global pause, are capped per call and per request, and are never applied to a daily 429. Mandatory local V0 on every released answer at every ladder level. Buffer, verify, release: the user-visible first token is the release time and the model's own first token is logged separately. Open-loop load with the scheduling lag of every launch logged. Invariants I1 to I6 recomputed from `events.jsonl`.

**Deviations (deliberate).**
1. *Sliding 60 s windows instead of refill-rate buckets* for requests and tokens. A refill bucket with a one-minute burst can send up to twice the limit inside one 60 s window; a window log cannot. Reserve-then-reconcile is unchanged. The real provider's window type (fixed or rolling) is still undocumented, hence the headroom.
2. *The discretionary share always admits one call per 60 s window.* At the documented 8K TPM a 15 % share is 1,200 tokens, smaller than one answer (about 2.5K), so a strict share would forbid every retry. The share caps what follows that first call.
3. *`limits_scale` (default 5) in stand-in scenarios.* At the documented limits only 3 to 4 answers fit per minute, which gives samples too small to say anything. Stand-in limits are the DOCUMENTED limits times this scale, a SIMULATED choice recorded in every manifest. `limits_scale: 1` runs at the documented numbers. Real runs require 1.
4. *Ladder level 1 changes nothing* in this build, because there is no V1 (optional LLM verify). Level 2 lowers k and max tokens; there is no smaller model.
5. *Level 4 is a cached answer or an honest busy message* for anything that needs the model, as designed. Retrieval is local and a refusal needs no model call, so a question the retriever refuses is still refused normally at level 4 (changed after the first `daily_exhausted` run showed free refusals being answered "busy"). A local extractive answer would also be possible when the budget is gone, but the design said busy, so it says busy.
6. *Extractive fallback is used* when V0 fails twice, at ladder level 3, and when the gateway cannot afford quota in time (`limiter_timeout`, `budget_denied`, retries exhausted on 429s). Provider 5xx and timeouts are reported as `error` or `timeout`, not hidden behind an extractive answer. Extractive answers count as `ok` with `answer_mode=extractive`, and summaries report them as degraded.
7. *Status mapping:* shed at the door or deadline passed means `timeout`; 429s, no affordable quota or exhausted daily budget means `rate_limited`; everything else means `error`. The finer reason is in the `done` event (`fail_stage`, `fail_reason`, `shed_reason`). A V0 refusal ("could not produce an answer that passed the checks") is `ok` with `answer_mode=refusal` and counts as a wrong refusal for an answerable question in the quality study.
8. *Not built:* coalescing (single-flight), adaptive N, LLM rewrite, V1, scheduler ablation. The verified-answer cache exists but is off in every scenario, since 20 questions repeat in a load run and a cache would inflate throughput.
9. *The one-reformulation loop exists but is off by default* (`reformulate_once`). Dropping common words raises query-term coverage, which loosens the abstain rule the retrieval study measured, so it needs its own measurement before being switched on. The regeneration loop after a V0 failure is on.
10. *`answer_grade` is always blank* in this phase. Grading is the quality study's job and is never invented.

**Assumptions to verify on the first real call** (none is documented in `limits.yaml`): that Groq accepts `reasoning_effort: low` (set in `relay.yaml`; delete if rejected); where streaming usage appears (the client reads top-level `usage` and `x_groq.usage`, and marks the call `usage_source=estimated` if neither is present); whether `stream_options.include_usage` is accepted (off for real runs by default); whether `max_completion_tokens` counts toward TPM up front (the gateway reserves it; so does the stand-in, which is a SIMULATED choice); whether rejected requests count toward RPM (the gateway counts them, the stand-in does not); the daily reset time (the ledger assumes the UTC date). The minute-vs-day 429 classifier is a guess (SIMULATED (guess)) and logs how each classification was made.

**Known limitations.**
- V0 check 5 is word overlap, not entailment; its threshold (0.5) is uncalibrated. It can pass a sentence built from the source's words that means something else, and reject a faithful paraphrase.
- The stand-in's replies are templated and quote the first source, so no quality number may come from stand-in runs. Its failures are independent coin flips; real incidents are correlated.
- Through `httpx.ASGITransport` (used only in tests) streams are buffered, so no timing is taken from tests. Runs use a real local server.
- Failed calls report no provider usage. Their tokens are tracked separately as `tokens_unaccounted_est` (an estimate) and are not added to `tokens_in/out`, which hold provider-reported usage only. The daily ledger does count a conservative estimate for them.
- The client-side daily ledger counts only this tool's calls; anything else on the account is invisible to it.
- Capacity ("rate_x") is arithmetic on the limits and an assumed 2,200 tokens per answer, not a measurement.
