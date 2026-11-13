import type { RequestRow, TraceEvent } from "./types";

/** One request's life, rebuilt from its own events: when each stage began and ended and what the log says happened.
 *  Nothing is inferred beyond the events; a stage that did not occur is simply absent. */

export type StageKind = "wait" | "work" | "retry" | "mark";

export interface Stage {
  key: string;
  label: string;
  kind: StageKind;
  start: number; // ms after arrival
  end: number | null; // null for a single moment
  lines: string[];
}

const s = (ms: number): string => (ms >= 1000 ? `${(ms / 1000).toFixed(ms >= 10000 ? 1 : 2)} s` : `${ms.toFixed(ms < 10 ? 1 : 0)} ms`);
const asList = (v: unknown): string => (Array.isArray(v) ? v.join(", ") : String(v));

export function buildLifecycle(events: TraceEvent[]): Stage[] {
  if (events.length === 0) return [];
  const t0 = (events.find((e) => e.event === "arrive") ?? events[0]).t_mono;
  const rel = (t: number) => (t - t0) * 1000;
  const out: Stage[] = [];
  let toolStart: { t: number; attempt: number } | null = null;
  let limiterStart: number | null = null;
  let send: Stage | null = null;
  let sleepStart: { t: number; ms: number } | null = null;
  let lastWorkEnd = 0;
  let model: Stage | null = null;

  for (const e of events) {
    const t = rel(e.t_mono);
    switch (e.event) {
      case "arrive":
        out.push({ key: "arrive", label: "Arrived", kind: "mark", start: 0, end: null, lines: [`question ${e.question_id ?? "?"}, retrieval setting ${e.setting ?? "?"}, patience ${e.deadline_s ?? "?"} s`] });
        break;
      case "shed":
        out.push({ key: "shed", label: "Shed at the door", kind: "mark", start: t, end: null, lines: [`reason: ${e.reason}`, `queue ${e.queue_len ?? "?"}, in flight ${e.inflight ?? "?"}`] });
        break;
      case "slot_acquired": {
        const wait = Number(e.wait_ms ?? t);
        out.push({ key: "admission", label: "Admission queue", kind: "wait", start: Math.max(0, t - wait), end: t, lines: [`waited ${s(wait)} for a free slot`, `queue ${e.queue_len ?? "?"}, in flight ${e.inflight ?? "?"} when it got in`] });
        lastWorkEnd = t;
        break;
      }
      case "ladder":
        out.push({ key: "ladder", label: `Degradation ladder: level ${e.level}`, kind: "mark", start: t, end: null, lines: [e.level === 0 ? "no pressure: full retrieval and a full-length answer" : "pressure on the budgets: the pipeline may use fewer pieces or skip the model", `pressure ${JSON.stringify(e.pressure ?? {})}`] });
        break;
      case "tool_start":
        toolStart = { t, attempt: Number(e.attempt ?? 1) };
        break;
      case "tool_end": {
        const begin = toolStart?.t ?? t;
        out.push({ key: `tool${out.length}`, label: toolStart && toolStart.attempt > 1 ? "Retrieval retry" : "Retrieval (tool call)", kind: e.ok === false ? "retry" : "work", start: begin, end: t, lines: [e.ok === false ? `failed: ${e.error}` : `searched the papers in ${s(Number(e.duration_ms ?? t - begin))}`] });
        lastWorkEnd = t;
        toolStart = null;
        break;
      }
      case "retrieval": {
        const last = [...out].reverse().find((x) => x.key.startsWith("tool"));
        const lines = [`${(e.hits ?? []).length} pieces: ${asList(e.hits ?? [])}`, `coverage ${e.coverage}${e.abstain ? `, abstain (${e.abstain_reason})` : ", no abstain"}`];
        if (last) last.lines.push(...lines);
        else out.push({ key: "retrieval", label: "Retrieval", kind: "mark", start: t, end: null, lines });
        break;
      }
      case "decide":
        out.push({ key: "decide", label: e.branch === "draft" ? "Decision: answer from the pieces" : "Decision: refuse (no model call)", kind: "mark", start: t, end: null, lines: [e.reason ? `reason: ${e.reason}` : "the retrieved evidence was enough to draft an answer", `coverage ${e.coverage ?? "?"}`] });
        break;
      case "limiter_wait_start":
        limiterStart = t;
        break;
      case "limiter_wait_end": {
        const begin = limiterStart ?? t - Number(e.waited_ms ?? 0);
        out.push({ key: `limiter${out.length}`, label: "Gateway: waiting for the rate limiter", kind: "wait", start: begin, end: t, lines: [`waited ${s(Number(e.waited_ms ?? t - begin))} for room in the 60 s token and request windows`] });
        lastWorkEnd = t;
        limiterStart = null;
        break;
      }
      case "send":
        send = { key: `call${out.length}`, label: `Model call: ${e.purpose ?? "draft"}, attempt ${e.attempt ?? 1}`, kind: Number(e.attempt ?? 1) > 1 || e.purpose === "regenerate" ? "retry" : "work", start: t, end: null, lines: [`reserved ${e.reserved_tokens ?? "?"} tokens in the gateway before sending`] };
        out.push(send);
        model = send;
        break;
      case "call_end": {
        const stage = send ?? model;
        const lines = [
          e.ok ? `answered in ${s(Number(e.duration_ms ?? 0))}` : `failed after ${s(Number(e.duration_ms ?? 0))}: ${e.error_kind}${e.status_code ? ` (HTTP ${e.status_code})` : ""}`,
          `tokens: ${e.tokens_in ?? 0} in, ${e.tokens_out ?? 0} out${e.reasoning_tokens !== undefined && e.reasoning_tokens !== null ? ` (${e.reasoning_tokens} reasoning)` : ""}${e.usage_source ? `, usage from the ${e.usage_source}` : ""}`,
        ];
        if (e.retry_after_s) lines.push(`retry-after ${e.retry_after_s} s`);
        if (stage) {
          stage.end = t;
          stage.lines.push(...lines);
          if (!e.ok) stage.kind = "retry";
        }
        lastWorkEnd = t;
        send = null;
        break;
      }
      case "model_first_token":
        if (model) model.lines.push(`first token from the model after ${s(Number(e.ttft_ms ?? 0))}`);
        break;
      case "retry_sleep_start":
        sleepStart = { t, ms: Number(e.sleep_ms ?? 0) };
        break;
      case "retry_sleep_end": {
        const b = sleepStart?.t ?? t;
        out.push({ key: `sleep${out.length}`, label: "Retry back-off", kind: "retry", start: b, end: t, lines: [`slept ${s(sleepStart?.ms ?? t - b)} before trying again`] });
        lastWorkEnd = t;
        sleepStart = null;
        break;
      }
      case "verify_result":
        out.push({
          key: `verify${out.length}`,
          label: `Verification (attempt ${e.attempt})`,
          kind: "work",
          start: Math.min(lastWorkEnd, t),
          end: t,
          lines: [e.passed ? "passed every check" : `failed: ${asList(e.failures ?? [])}`, ...(e.details && e.details.length ? [`offending text: ${asList(e.details)}`] : []), `citations used: ${asList(e.cited ?? []) || "none"}`],
        });
        lastWorkEnd = t;
        break;
      case "loop_regenerate":
        out.push({ key: `regen${out.length}`, label: "Regeneration requested", kind: "retry", start: t, end: null, lines: [`the first draft failed verification (${asList(e.reasons ?? [])}); asking the model once more with a stricter prompt`] });
        break;
      case "call_failed":
        out.push({ key: `failed${out.length}`, label: "Gateway gave up on a call", kind: "retry", start: t, end: null, lines: [`reason: ${e.failure}${e.last_error ? `, last error ${e.last_error}` : ""}`] });
        break;
      case "degrade":
        out.push({ key: `degrade${out.length}`, label: "Fallback: quote a sentence instead of a model answer", kind: "retry", start: t, end: null, lines: [`reason: ${e.reason ?? `ladder level ${e.level}`}`, "no model call is used for this answer"] });
        lastWorkEnd = t;
        break;
      case "busy":
        out.push({ key: "busy", label: "Answered \"busy\"", kind: "mark", start: t, end: null, lines: [`reason: ${e.reason}`] });
        break;
      case "first_token":
        out.push({ key: "release", label: "Released to the person", kind: "mark", start: t, end: null, lines: [`answer mode: ${e.mode}`, `first token at ${s(Number(e.ms ?? t))}`] });
        break;
      case "done":
        out.push({ key: "done", label: `Finished: ${e.status}`, kind: "mark", start: t, end: null, lines: [`total ${s(Number(e.total_ms ?? t))}, ${e.retries ?? 0} retries, ${e.attempts ?? 0} model calls, ${e.tokens_in ?? 0} tokens in, ${e.tokens_out ?? 0} out`, ...(e.fail_reason ? [`failure: ${e.fail_stage ?? "?"}: ${e.fail_reason}`] : []), ...(e.shed_reason ? [`shed: ${e.shed_reason}`] : [])] });
        break;
      default:
        break;
    }
  }
  return out;
}

/** Plain sentences about why a request was slow or did not succeed, each backed by an event or a row field. */
export function explain(row: RequestRow | null, events: TraceEvent[]): string[] {
  const out: string[] = [];
  if (!row || events.length === 0) return out;
  const total = row.total_ms;
  const parts: [string, number][] = [["waiting (queue and rate limiter)", row.waiting_ms], ["working (retrieval, model calls, checks)", row.working_ms], ["retrying (failed calls and back-off)", row.retrying_ms]];
  const [big, bigMs] = [...parts].sort((a, b) => b[1] - a[1])[0];
  if (total > 0 && bigMs / total >= 0.5) out.push(`Most of the ${s(total)} was ${big}: ${s(bigMs)}.`);
  else if (total > 0) out.push(`The ${s(total)} was split: waiting ${s(row.waiting_ms)}, working ${s(row.working_ms)}, retrying ${s(row.retrying_ms)}.`);

  const adm = events.find((e) => e.event === "slot_acquired");
  if (adm && Number(adm.wait_ms ?? 0) >= 1000) out.push(`It waited ${s(Number(adm.wait_ms))} in the admission queue.`);
  events.filter((e) => e.event === "limiter_wait_end" && Number(e.waited_ms ?? 0) >= 1000).forEach((e) => {
    const after = events.filter((x) => x.event === "send" && x.t_mono > e.t_mono)[0];
    out.push(`It waited ${s(Number(e.waited_ms))} for the rate limiter${after ? ` before the ${String(after.purpose ?? "draft")} call (attempt ${after.attempt ?? 1})` : ""}.`);
  });
  const calls = events.filter((x) => x.event === "call_end");
  const slow = calls.filter((c) => Number(c.duration_ms ?? 0) >= 3000);
  slow.forEach((c) => out.push(`One model call took ${s(Number(c.duration_ms))}${c.ok ? "" : ` and failed (${c.error_kind})`}.`));
  const failed = calls.filter((c) => !c.ok);
  const n429 = failed.filter((c) => String(c.error_kind ?? "").startsWith("rate_limit")).length;
  if (n429) out.push(`The service answered 429 (rate limit) ${n429} time${n429 === 1 ? "" : "s"}.`);
  if (failed.length - n429 > 0) out.push(`${failed.length - n429} other failed call${failed.length - n429 === 1 ? "" : "s"}: ${Array.from(new Set(failed.map((c) => String(c.error_kind)))).filter((k) => !k.startsWith("rate_limit")).join(", ")}.`);
  const verifies = events.filter((x) => x.event === "verify_result");
  const bad = verifies.filter((v) => !v.passed && v.attempt !== 3);
  if (bad.length) out.push(`${bad.length === 1 ? "A draft" : `${bad.length} drafts`} failed verification (${Array.from(new Set(bad.flatMap((v) => (v.failures ?? []) as string[]))).join(", ")}).`);
  if (events.some((x) => x.event === "loop_regenerate")) {
    const second = verifies.find((v) => v.attempt === 2);
    out.push(second ? `It asked the model again; the second draft ${second.passed ? "passed" : "failed too"}.` : "It asked the model for a second draft.");
  }
  const deg = events.find((x) => x.event === "degrade");
  if (deg) out.push(`It released a quoted sentence instead of a model answer (${deg.reason ?? `ladder level ${deg.level}`}).`);
  const shed = events.find((x) => x.event === "shed");
  if (shed) out.push(`It was shed at the door (${shed.reason}).`);
  const done = events.find((x) => x.event === "done");
  if (done && done.status !== "ok") out.push(`It ended as ${done.status}${done.fail_reason ? `: ${done.fail_stage ?? "?"}, ${done.fail_reason}` : ""}.`);
  if (row.first_token_ms !== null && total - row.first_token_ms < 5) out.push("All of this happened before the first token: answers are verified and then released whole.");
  return out;
}
