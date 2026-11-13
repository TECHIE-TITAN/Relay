# The Relay: write-up (skeleton)

Status legend: **[FILL]** = needs numbers from a run not finished yet; **[YOURS]** = must be your own words or opinion, I will not write it; text without a marker is drafted from evidence we already have. Limit: 2 pages of writing, so this will be cut down once the numbers are in. Every number in the results sections comes from `results.csv` through `report/tables.md`.

## 1. What I built and why

[Draft] A Python pipeline with ten steps: admit (bounded FIFO door queue), understand, ladder (decide how much work the current pressure allows), retrieve (a tool call with one retry), decide (answer, or refuse with no model call), draft (Groq `openai/gpt-oss-20b`), verify (mandatory local check), regenerate once, extractive fallback or honest refusal, release. Branches: refuse-or-answer, and the ladder. Loop: regenerate once after a failed check.

**The decision that shaped it most:** on this account the binding limit is tokens per minute (8,000 TPM, 30 RPM, 1,000 RPD, 200K TPD), which allows about 3.6 answers a minute, so I treated tokens, not requests, as the scarce resource. Every model call reserves its prompt plus its full `max_tokens` before it is sent (the real service charged the reservation up front in one observation), retries and regenerations draw on a separate, capped share, and a degradation ladder trades answer quality for tokens before it trades away requests. Cost of that choice: the gateway is conservative, so it leaves some capacity unused.

Verification runs on every answer and is never shed: a reply is buffered, checked (citations exist, every sentence cited, quotes verbatim, numbers and names grounded, enough word overlap with the source), and only then released. So the user-visible first token is the release time. The check is lexical, not entailment (stated limitation).

## 2. The real service and my stand-in

### Table 1: Limits found in the real service (account: Groq free tier, model `openai/gpt-oss-20b`)

| Limit | Value found | How I found it | Confidence |
|---|---|---|---|
| Requests per minute | 30 | Limit probe: 36 tiny calls in under a minute; the 31st returned 429 "Limit 30, Used 30, Requested 1" | High |
| Tokens per minute | 8,000 (refills continuously, about 133 tokens/s) | `x-ratelimit-limit-tokens: 8000` on every response; `reset-tokens` of 11.3 s after a 1.6K-token call. Never exceeded on purpose | High (reported, not triggered) |
| Requests per day | 1,000 | `x-ratelimit-limit-requests: 1000`; `remaining` fell by 1 per call; `reset-requests` grew by 86.4 s per call. Not triggered | High (reported, not triggered) |
| Tokens per day | 200,000 | Account console Limits page; no response header carries it. Not triggered by design | Medium |
| Requests in flight at once | not measured beyond 4 | 1, 2 and 4 concurrent tiny calls (3 batches each) all succeeded, no latency growth | Low |
| What it returns when a limit is hit | HTTP 429, a message naming the limit ("... on requests per minute (RPM) ..."), `retry-after: 2`, request-limit headers but no token headers | The same probe (OBSERVED-TRIGGERED; RPM only) | High for RPM, not observed for the others |
| Time until it recovers | 2.4 s after the first 429 (one retry after the 2 s hint succeeded) | Same probe, n=1 | Medium |

**Open question, stated honestly:** whether the token limit is charged on the reservation (prompt + `max_tokens`) or on actual usage. The single probe call matched the reservation (363 charged for 100 used); a later pair of calls matched actual usage. I keep the conservative reading.

[FILL after calibration check] **Stand-in.** A local FastAPI server with the same endpoint and headers, streaming in chunks, sliding-window RPM/TPM/RPD/TPD limits that return 429 with the same message shape, injected 5xx, hangs, slow successes and mid-stream drops. **Calibrated from real data:** median time to first token 580 ms (40 real calls), no slowdown with 1 to 4 concurrent calls (so the concurrency slowdown is off by default), header and 429 shape. **Not calibrated, SIMULATED choices:** decode speed (150 tokens/s; the real replies were too short to measure), the failure and hang rates, the tail width, and the limits, which scenarios scale by 5 (recorded in every manifest). **Least like the real service:** it replies with a templated quote (no reasoning tokens, which were 43 to 46% of real output), its failures are independent coin flips while real incidents are correlated, and it has no account-wide daily behaviour, so the daily-limit scenario is simulated and was never triggered on the real service.

## 3. Results

[FILL] Tables 2a and 2b from `report/tables.md`, copied here. State for each row real or stand-in, n requests, and which percentiles (p50, p95; p99 only with at least 100 requests).

[FILL] **A number I could not have guessed:** candidates from the data: reasoning tokens are 43 to 46% of the output tokens even at `reasoning_effort: low`; the same request varied 2 to 3 times in time to first token at identical token counts; identical prompts gave identical token counts across three runs. Pick one and show the rows.

[FILL] Answer quality (20 questions I wrote), tokens per answer, in Table 3 below.

## 4. Where the time goes

[FILL] One slow request from `report/tables.md` ("Slowest successful stand-in request"): its waiting, working and retrying split and its timeline. Because the answer is buffered and verified before release, all of a slow request's delay comes before the first token; the table gives the exact milliseconds after the first token. Show the events that prove the split.

## 5. When a limit is hit

[Draft; numbers FILL] **Per minute.** Before each call the gateway reserves tokens in a sliding 60 s window. If the window is full, requests wait their turn (FIFO) and are delayed; a request whose deadline cannot cover the wait fails fast as rate-limited. After a 429 the gateway pauses all dispatch and cuts its rate. As pressure rises, the ladder first gives later requests a smaller context and a lower `max_tokens`, then an extractive answer quoted from the sources with no model call (a weaker answer), then a busy message for anything that needs the model. Free refusals are never delayed. **Per day.** A daily 429 is never retried; the client-side ledger marks the day exhausted, and questions that need the model get the busy message, while questions the papers cannot answer still get their free refusal. The daily scenario is SIMULATED; I did not run the real account out of quota on purpose. **Who:** delayed = everyone behind in the queue; dropped = requests whose deadline cannot cover the wait; weaker = requests that arrive while the ladder is at level 2 or 3.

## Retrieval: quality versus cost

[FILL] Table 3 (settings S1, S2, S3 on the real model; S4 retrieval-only) and Table 3b. Choose one setting and say why (quality per token, the abstain behaviour, time to first token). What retrieval does when the limit is close: ladder level 2 fetches fewer pieces (smaller k), level 3 skips the model and quotes a sentence.

## How this library will mislead a naive retriever

1. **A question that needs two papers is answered from one.** q13 "What does Moshi generate hierarchically, and what does H-Net hierarchically compress?": all four retrieved chunks came from the H-Net paper and none from Moshi (evidence recall 0.0 at S2), probably because the H-Net paper uses "hierarchical" far more than Moshi does (a guess; I did not count).
2. **Same words, different system.** q01 "How many identical layers are in the Transformer's encoder stack?": the rank-2 chunk was an H-Net passage about encoder and decoder layers. q08 "How does Moshi represent the user's speech ...?": a Related Work sentence about the earlier system dGSLM matched "full-duplex dialogue" and "user speech" and was quoted as if it described Moshi. In both cases the words match and the subject does not.
3. **Word overlap cannot tell a missing topic from missing words.** q04 (answerable) was refused for free because only 22% of its query words appeared in any chunk (threshold 0.5), while q17, which asks about something the papers do not contain ("the maximum number of concurrent users supported by the Transformer architecture ..."), scored exactly 0.50 and reached the model. Related: PDF front matter (title, author names, e-mails) is full of question words and was quoted as an answer until I excluded sentences with an e-mail address.

## 6. Retries

[FILL numbers from retry_naive vs retry_budgeted] **Policy:** a retry or regeneration is a "discretionary" call: it stays in the request's slot, draws on a capped share of the window (one call per window is always allowed), fails fast when the share is empty, honours the global pause, is capped per call (2) and per request (5 attempts), and a daily 429 is never retried. **Case where retrying made things worse:** the naive policy (no limiter, no share) against the hot stand-in scenario sent 96 calls for 89 requests, 36 of them 429s, leaving about 15K tokens with no recorded outcome; the budgeted policy sent 54 calls with no 429. In the earlier stand-in run the naive policy still had the lower p95, so I report that the cost of retrying is quota, not latency, at that load. Re-state with the final calibrated runs.

## 7. What the person sees

At 3pm the daily budget is gone. A question that needs the model gets: "The service is at its limit right now. Please try again in a few minutes." A question the papers cannot answer still gets the usual free refusal: "The three papers do not contain enough information to answer this question." No answer is cached or quoted at that level (the design chose an honest busy message over a degraded answer). What I give up: a person who would have got an extractive quote now gets nothing.

## 8. What I'm unsure about, what I left out, what I learned

[YOURS, with my evidence] **Least certain assumption:** how the token limit is charged (reservation or actual). If it is actual usage, the gateway is too conservative and leaves capacity unused; if it is something else (for example `max_tokens` charged but not refunded), the daily budget runs out earlier than the ledger says. A second one: my client-side daily ledger assumes the day resets at 00:00 UTC, but the response headers say the daily request quota refills continuously (`reset-requests` was 1h4m48s with 45 requests used, which is exactly 45 x 86.4 s), so the real reset is probably rolling; if so, the ledger would show a fresh day while the provider still counts yesterday's calls. Other uncertain parts: the 429 minute-versus-day classifier is a guess from one observation of the minute case; the V0 check is word overlap, not entailment, with an uncalibrated 0.5 threshold; the stand-in's decode speed and failure rates are choices, not measurements.
**Two things I deliberately did not build** [YOURS to confirm]: an LLM-based verifier (V1), and request coalescing / adaptive concurrency.
**One thing that was new to me** [YOURS, own words].

## Your feedback on Vaani

[YOURS] Visit https://www.vaani.dev/ and write a few lines: what stood out, what confused you, one thing you would change. I will not write this.

## How I worked with the assistant

Full real history: `report/conversation_history.md` (made by `export-conversation`; secrets redacted and long tool outputs clipped, both stated in its header).

[YOURS] The three prompts that mattered most and why; one thing the assistant got wrong that you caught and how; one decision where you overruled it; what is distinctly yours.

<!-- Candidates drawn from the real history (delete this block): things the assistant got wrong that you caught, or that a check caught:
 - gave a preflight command (`retrieve`) that does not exist, and a guard that checked per question instead of per model call and ignored concurrent reservations (you asked for the full-reservation and atomicity check);
 - described a test as "regeneration used" when the pilot had regeneration off (you asked);
 - the pilot showed the verifier rejected a correct answer (a name from the question) and the fallback quoted another system and then the title page;
 - level 4 said "busy" even for free refusals until a stand-in run showed it.
 Decisions where you overruled or redirected it: replaced q13 with q08 for the token pilot and kept q13 for a separate experiment; chose option A (relevance rules) and then "stop tuning"; required the 60K study cap and the 6K per-request stop; disallowed running the stand-in load alongside the real run. -->
