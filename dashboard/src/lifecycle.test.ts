import { describe, expect, it } from "vitest";
import { buildLifecycle, explain } from "./lifecycle";
import type { RequestRow, TraceEvent } from "./types";

let n = 0;
const ev = (event: string, t: number, extra: Record<string, any> = {}): TraceEvent => ({ request_id: "r", event, t_mono: t, ...extra, _n: n++ });

// The shape of the real slowest request (S3, q15): a draft, a failed check, a regeneration that waited for the token
// window, a second failed check, and a quoted sentence as the fallback.
const SLOW: TraceEvent[] = [
  ev("arrive", 0, { question_id: "q15", setting: "S3", deadline_s: 90 }),
  ev("slot_acquired", 0.0001, { wait_ms: 0, queue_len: 0, inflight: 1 }),
  ev("ladder", 0.0002, { level: 0, pressure: { tpm: 0.3 } }),
  ev("tool_start", 0.0003, { attempt: 1 }),
  ev("tool_end", 0.002, { ok: true, duration_ms: 1.7 }),
  ev("retrieval", 0.0021, { hits: ["attention:S3:0004", "moshi:S3:0010"], coverage: 0.57, abstain: false }),
  ev("decide", 0.0022, { branch: "draft", coverage: 0.57, n_hits: 2 }),
  ev("limiter_wait_start", 0.003),
  ev("limiter_wait_end", 0.4643, { waited_ms: 461 }),
  ev("send", 0.4646, { attempt: 1, purpose: "draft", reserved_tokens: 2400 }),
  ev("call_end", 1.3026, { ok: true, duration_ms: 836.3, tokens_in: 1500, tokens_out: 90, reasoning_tokens: 40, usage_source: "provider" }),
  ev("model_first_token", 1.3027, { ttft_ms: 700 }),
  ev("verify_result", 1.3072, { attempt: 1, passed: false, failures: ["uncited_claim"], details: ["uncited_claim: the first sentence"], cited: ["S1"] }),
  ev("loop_regenerate", 1.3073, { reasons: ["uncited_claim"] }),
  ev("limiter_wait_start", 1.3074),
  ev("limiter_wait_end", 58.8886, { waited_ms: 57581 }),
  ev("send", 58.889, { attempt: 2, purpose: "regenerate", reserved_tokens: 2400 }),
  ev("call_end", 59.7498, { ok: true, duration_ms: 858.1, tokens_in: 1560, tokens_out: 95 }),
  ev("verify_result", 59.7543, { attempt: 2, passed: false, failures: ["uncited_claim"], details: [], cited: ["S1"] }),
  ev("degrade", 59.7545, { reason: "v0_failed", to: "extractive" }),
  ev("verify_result", 59.7599, { attempt: 3, passed: true, failures: [], details: [], cited: ["S2"] }),
  ev("first_token", 59.76, { ms: 59760, mode: "extractive" }),
  ev("done", 59.7603, { status: "ok", answer_mode: "extractive", total_ms: 59760.3, retries: 0, attempts: 2, tokens_in: 3060, tokens_out: 185 }),
];

const ROW: RequestRow = {
  request_id: "r", question_id: "q15", start_time: "t", first_token_ms: 59760, total_ms: 59760.3, status: "ok", retries: 0, tokens_in: 3060, tokens_out: 185,
  arrive_t: 0, waiting_ms: 58042, working_ms: 1707, retrying_ms: 0, unaccounted_ms: 11.5,
};

describe("buildLifecycle", () => {
  const stages = buildLifecycle(SLOW);
  const find = (re: RegExp) => stages.filter((x) => re.test(x.label));

  it("lists the stages in the order they happened", () => {
    const labels = stages.map((x) => x.label);
    expect(labels[0]).toBe("Arrived");
    expect(labels.indexOf("Admission queue")).toBeLessThan(labels.findIndex((l) => l.startsWith("Retrieval")));
    expect(labels.findIndex((l) => l.startsWith("Decision"))).toBeLessThan(labels.findIndex((l) => l.startsWith("Model call: draft")));
    expect(labels[labels.length - 1]).toBe("Finished: ok");
    const starts = stages.map((x) => x.start);
    expect([...starts].sort((a, b) => a - b)).toEqual(starts); // no stage starts before an earlier one
  });

  it("measures the two waits and the two model calls from their own events", () => {
    const waits = find(/rate limiter/);
    expect(waits.map((w) => Math.round((w.end as number) - w.start))).toEqual([461, 57581]);
    const calls = find(/^Model call/);
    expect(calls.map((c) => [c.label, c.kind])).toEqual([["Model call: draft, attempt 1", "work"], ["Model call: regenerate, attempt 2", "retry"]]);
    expect(Math.round((calls[0].end as number) - calls[0].start)).toBe(838);
    expect(calls[0].lines.join(" ")).toMatch(/reserved 2400 tokens/);
    expect(calls[0].lines.join(" ")).toMatch(/first token from the model after 700 ms/);
    expect(calls[0].lines.join(" ")).toMatch(/1500 in, 90 out \(40 reasoning\), usage from the provider/);
  });

  it("keeps the retrieved pieces and the verification outcome of each attempt", () => {
    expect(find(/^Retrieval/)[0].lines.join(" ")).toMatch(/attention:S3:0004, moshi:S3:0010/);
    const v = find(/^Verification/);
    expect(v.map((x) => x.label)).toEqual(["Verification (attempt 1)", "Verification (attempt 2)", "Verification (attempt 3)"]);
    expect(v[0].lines.join(" ")).toMatch(/failed: uncited_claim/);
    expect(v[0].lines.join(" ")).toMatch(/offending text: uncited_claim: the first sentence/);
    expect(v[2].lines[0]).toBe("passed every check");
    expect(find(/Fallback/)[0].lines[0]).toBe("reason: v0_failed");
  });

  it("is empty for no events and has no stage for what did not happen", () => {
    expect(buildLifecycle([])).toEqual([]);
    expect(buildLifecycle(SLOW.slice(0, 3)).some((x) => /Model call/.test(x.label))).toBe(false);
  });
});

describe("explain", () => {
  const lines = explain(ROW, SLOW);
  it("says where the time went and why, using only logged facts", () => {
    expect(lines[0]).toMatch(/Most of the 59\.8 s was waiting \(queue and rate limiter\): 58\.0 s\./);
    expect(lines.join("\n")).toMatch(/waited 57\.6 s for the rate limiter before the regenerate call \(attempt 2\)/);
    expect(lines.join("\n")).toMatch(/failed verification \(uncited_claim\)/);
    expect(lines.join("\n")).toMatch(/second draft failed too/);
    expect(lines.join("\n")).toMatch(/quoted sentence instead of a model answer \(v0_failed\)/);
    expect(lines[lines.length - 1]).toMatch(/before the first token/);
  });
  it("does not invent causes for a quick, clean request", () => {
    const quick: TraceEvent[] = [ev("arrive", 0), ev("slot_acquired", 0.001, { wait_ms: 1 }), ev("send", 0.01, { attempt: 1, purpose: "draft" }), ev("call_end", 0.5, { ok: true, duration_ms: 490 }), ev("verify_result", 0.51, { attempt: 1, passed: true, cited: ["S1"] }), ev("done", 0.52, { status: "ok", total_ms: 520 })];
    const out = explain({ ...ROW, total_ms: 520, first_token_ms: 520, waiting_ms: 1, working_ms: 515, retrying_ms: 0 }, quick);
    expect(out.join(" ")).not.toMatch(/rate limiter|429|failed|quoted|shed/);
    expect(out[0]).toMatch(/Most of the 520 ms was working/);
  });
  it("names a shed request and a 429", () => {
    const shed: TraceEvent[] = [ev("arrive", 0), ev("shed", 0.01, { reason: "queue_full" }), ev("done", 0.02, { status: "timeout", total_ms: 20, shed_reason: "queue_full", fail_stage: "admit", fail_reason: "shed:queue_full" })];
    expect(explain({ ...ROW, status: "timeout", first_token_ms: null, total_ms: 20, waiting_ms: 20, working_ms: 0, retrying_ms: 0 }, shed).join(" ")).toMatch(/shed at the door \(queue_full\).*ended as timeout/);
    const limited: TraceEvent[] = [ev("arrive", 0), ev("send", 0.1, { attempt: 1 }), ev("call_end", 0.3, { ok: false, error_kind: "rate_limit_minute", duration_ms: 200 }), ev("done", 0.4, { status: "rate_limited", total_ms: 400 })];
    expect(explain({ ...ROW, status: "rate_limited", first_token_ms: null, total_ms: 400, waiting_ms: 0, working_ms: 0, retrying_ms: 200 }, limited).join(" ")).toMatch(/429 \(rate limit\) 1 time\./);
  });
});
