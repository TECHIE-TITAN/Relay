import { describe, expect, it } from "vitest";
import { LiveModel } from "./live";
import type { TraceEvent } from "./types";

let seq = 0;
const ev = (request_id: string | null, event: string, t_mono: number, extra: Record<string, any> = {}): TraceEvent => ({ request_id, event, t_mono, ...extra, _n: seq++ });

function play(m: LiveModel, events: TraceEvent[]): void {
  events.forEach((e) => m.ingest(e));
}

describe("request states come from events and from nothing else", () => {
  it("walks a request through the stages", () => {
    const m = new LiveModel(5);
    const a = (e: string, t: number, x: Record<string, any> = {}) => m.ingest(ev("r1", e, t, x));
    a("arrive", 0, { question_id: "q01" });
    expect([m.reqs.get("r1")!.node, m.reqs.get("r1")!.state]).toEqual(["admission", "queued"]);
    a("slot_acquired", 0.1, { wait_ms: 100 });
    expect([m.reqs.get("r1")!.node, m.reqs.get("r1")!.state]).toEqual(["retrieve", "processing"]);
    a("limiter_wait_start", 0.2);
    expect([m.reqs.get("r1")!.node, m.reqs.get("r1")!.state]).toEqual(["gateway", "queued"]);
    a("limiter_wait_end", 0.9, { waited_ms: 700 });
    expect(m.reqs.get("r1")!.state).toBe("processing"); // first attempt: not a retry
    a("send", 1.0, { attempt: 1, purpose: "draft" });
    expect([m.reqs.get("r1")!.node, m.reqs.get("r1")!.state]).toEqual(["model", "processing"]);
    a("call_end", 1.5, { ok: true });
    expect(m.reqs.get("r1")!.node).toBe("verify");
    a("verify_result", 1.51, { passed: true });
    a("first_token", 1.52, { ms: 1520, mode: "llm" });
    expect(m.reqs.get("r1")!.node).toBe("release");
    a("done", 1.53, { status: "ok", answer_mode: "llm", total_ms: 1530, retries: 0, tokens_in: 100, tokens_out: 20 });
    const r = m.reqs.get("r1")!;
    expect([r.done, r.state, r.qid, r.mode]).toEqual([true, "successful", "q01", "llm"]);
    expect(m.counts().finished).toEqual({ successful: 1, rejected: 0, failed: 0 });
    expect(m.counts().open).toBe(0);
  });

  it("shows a failed call and its retry as retrying, then success", () => {
    const m = new LiveModel(5);
    play(m, [
      ev("r2", "arrive", 0, { question_id: "q02" }),
      ev("r2", "slot_acquired", 0.1),
      ev("r2", "send", 0.2, { attempt: 1, purpose: "draft" }),
      ev("r2", "call_end", 0.7, { ok: false, error_kind: "server_error" }),
    ]);
    expect([m.reqs.get("r2")!.node, m.reqs.get("r2")!.state]).toEqual(["gateway", "retrying"]);
    play(m, [ev("r2", "retry_sleep_start", 0.71, { sleep_ms: 50 }), ev("r2", "send", 0.8, { attempt: 2, purpose: "draft" })]);
    expect([m.reqs.get("r2")!.node, m.reqs.get("r2")!.state]).toEqual(["model", "retrying"]); // the retried attempt is shown as retrying
    play(m, [ev("r2", "call_end", 1.2, { ok: true }), ev("r2", "done", 1.3, { status: "ok", total_ms: 1300, retries: 1 })]);
    expect(m.reqs.get("r2")!.state).toBe("successful");
    expect(m.reqs.get("r2")!.retryEvents).toBe(1);
  });

  it("shows a regeneration as retrying", () => {
    const m = new LiveModel(5);
    play(m, [ev("r3", "arrive", 0), ev("r3", "slot_acquired", 0.1), ev("r3", "send", 0.2, { attempt: 1, purpose: "draft" }), ev("r3", "call_end", 0.6, { ok: true }), ev("r3", "verify_result", 0.61, { passed: false }), ev("r3", "loop_regenerate", 0.62)]);
    expect([m.reqs.get("r3")!.node, m.reqs.get("r3")!.state, m.reqs.get("r3")!.regenerated]).toEqual(["gateway", "retrying", true]);
    play(m, [ev("r3", "limiter_wait_start", 0.63), ev("r3", "limiter_wait_end", 5.0), ev("r3", "send", 5.1, { attempt: 2, purpose: "regenerate" })]);
    expect(m.reqs.get("r3")!.state).toBe("retrying");
  });

  it("classifies the three ways a request can end without succeeding", () => {
    const m = new LiveModel(5);
    play(m, [
      ev("s", "arrive", 0), ev("s", "shed", 0.01, { reason: "queue_full" }), ev("s", "done", 0.02, { status: "timeout", shed_reason: "queue_full", total_ms: 20 }),
      ev("b", "arrive", 0), ev("b", "done", 1, { status: "rate_limited", total_ms: 900 }),
      ev("t", "arrive", 0), ev("t", "done", 20, { status: "timeout", total_ms: 20000 }),
      ev("e", "arrive", 0), ev("e", "done", 2, { status: "error", total_ms: 2000 }),
    ]);
    const st = (id: string) => m.reqs.get(id)!.state;
    expect([st("s"), st("b"), st("t"), st("e")]).toEqual(["rejected", "rejected", "failed", "failed"]);
    expect(m.counts().finished).toEqual({ successful: 0, rejected: 2, failed: 2 });
    expect(m.summary()).toMatchObject({ n: 4, ok: 0, rejected: 2, failed: 2 });
  });

  it("counts requests per stage and state while they are open", () => {
    const m = new LiveModel(5);
    play(m, [ev("a", "arrive", 0), ev("b", "arrive", 0), ev("b", "slot_acquired", 0.1), ev("c", "arrive", 0), ev("c", "slot_acquired", 0.1), ev("c", "limiter_wait_start", 0.2)]);
    const c = m.counts();
    expect(c.nodes.admission.queued).toBe(1);
    expect(c.nodes.retrieve.processing).toBe(1);
    expect(c.nodes.gateway.queued).toBe(1);
    expect(c.open).toBe(3);
    expect(m.inNode("gateway").map((r) => r.id)).toEqual(["c"]);
  });
});

describe("run-level facts", () => {
  it("collects gateway samples and rate-limit events without inventing any", () => {
    const m = new LiveModel(5);
    play(m, [
      ev(null, "gateway_state", 1, { queue_len: 2, inflight: 4, tpm_used: 1000, tpm_limit: 40000 }),
      ev(null, "gateway_state", 2, { queue_len: 5, inflight: 4, tpm_used: 2000, tpm_limit: 40000 }),
      ev(null, "control", 3, { body: { rpm: 75 } }),
      ev(null, "limit_changed", 4, { what: "tpm", old: 40000, new: 20000 }),
      ev(null, "pause_start", 4.5, { reason: "429" }),
      ev("r", "arrive", 4.6), ev("r", "call_end", 5, { ok: false, error_kind: "rate_limit_minute", retry_after_s: 2 }),
    ]);
    expect(m.samples).toHaveLength(2);
    expect(m.limitLog.map((l) => l.event)).toEqual(["control", "limit_changed", "pause_start", "provider 429"]);
    expect(m.provider429).toBe(1);
    expect(m.summary()).toMatchObject({ peakQueue: 5, peakInflight: 4, provider429: 1 });
  });
});

describe("circuit breaker and pauses", () => {
  it("counts breaker openings (a request-level event) and pauses (a run-level event) separately", () => {
    const m = new LiveModel(5);
    play(m, [
      ev("a", "arrive", 0, { question_id: "q03" }),
      ev("a", "breaker_open", 1, { cooldown_s: 3 }),
      ev(null, "pause_start", 1.01, { reason: "breaker_open", seconds: 3 }),
      ev(null, "pause_start", 9, { reason: "429" }),
      ev("a", "daily_limit", 12, { tokens: 1 }),
    ]);
    expect(m.summary()).toMatchObject({ breakerOpens: 1, pauses: 2 });
    expect(m.limitLog.map((l) => l.event)).toEqual(["circuit breaker opened", "pause_start", "pause_start", "daily limit reached"]);
    expect(m.limitLog[0].note).toMatch(/cooldown 3 s \(seen by q03\)/);
    expect(m.series().reduce((s, b) => s + b.limit, 0)).toBe(4); // each one is a rate-limit event in its bucket
  });
  it("reports none rather than zero-by-assumption when the run logged none: the counts stay 0 and the log stays empty", () => {
    const m = new LiveModel(5);
    play(m, [ev("a", "arrive", 0), ev("a", "done", 1, { status: "ok", total_ms: 1000 })]);
    expect(m.summary()).toMatchObject({ breakerOpens: 0, pauses: 0, provider429: 0 });
    expect(m.limitLog).toEqual([]);
  });
});

describe("series and summary", () => {
  it("buckets arrivals, completions, retries and the latency of successful requests", () => {
    const m = new LiveModel(5);
    play(m, [
      ev("a", "arrive", 0), ev("b", "arrive", 1), ev("c", "arrive", 7),
      ev("a", "done", 2, { status: "ok", total_ms: 100, tokens_in: 10, tokens_out: 5 }),
      ev("b", "retry_sleep_start", 3), ev("b", "done", 4, { status: "ok", total_ms: 300, retries: 1, tokens_in: 10, tokens_out: 5 }),
      ev("c", "done", 8, { status: "ok", total_ms: 200, tokens_in: 10, tokens_out: 5 }),
    ]);
    const s = m.series();
    expect(s.map((b) => b.arrived)).toEqual([2, 1]);
    expect(s.map((b) => b.completed)).toEqual([2, 1]);
    expect(s[0].retries).toBe(1);
    expect(s[0].p50).toBe(200); // median of 100 and 300
    expect(s[1].p50).toBe(200);
    const sum = m.summary();
    expect(sum.n).toBe(3);
    expect(sum.okRate).toBe(1);
    expect(sum.retriesPerRequest).toBeCloseTo(1 / 3, 9);
    expect(sum.tokens).toBe(45);
    expect(sum.p50).toBe(200);
  });

  it("has no latency numbers before anything succeeded (null, not zero)", () => {
    const m = new LiveModel(5);
    play(m, [ev("a", "arrive", 0)]);
    expect(m.summary().p50).toBeNull();
    expect(m.series()[0].p50).toBeNull();
    expect(m.summary().okRate).toBeNull();
  });
});
