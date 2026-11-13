import { describe, expect, it } from "vitest";
import { byScenario, histogram, kpis, parseRows, throughputSeries } from "./aggregate";
import { percentile, isRealScenario } from "./stats";
import type { AggRow } from "./types";

function row(over: Partial<AggRow> & { offsetS?: number } = {}): AggRow {
  const base = Date.parse("2026-01-01T00:00:00.000+00:00") + (over.offsetS ?? 0) * 1000;
  const { offsetS, ...rest } = over;
  return {
    run_id: "r1",
    scenario: "steady",
    source: "stand-in",
    start_time: new Date(base).toISOString(),
    first_token_ms: "100",
    total_ms: "150",
    status: "ok",
    retries: "0",
    tokens_in: "100",
    tokens_out: "20",
    ...rest,
  };
}

describe("percentile", () => {
  it("is the type 7 definition", () => {
    expect(percentile([150, 250, 350], 95)).toBeCloseTo(340, 9);
    expect(percentile([10, 20, 30, 40], 50)).toBe(25);
    expect(percentile([7], 99)).toBe(7);
    expect(percentile([], 50)).toBeNull();
  });
});

describe("parseRows", () => {
  it("keeps a blank as null, never as zero", () => {
    const [r] = parseRows([row({ first_token_ms: "", total_ms: "", status: "error", tokens_in: "", tokens_out: null })]);
    expect(r.ft).toBeNull();
    expect(r.total).toBeNull();
    expect(r.tin).toBe(0); // tokens: no provider usage was reported for a failed call
  });
});

describe("kpis", () => {
  it("counts outcomes and takes latency from successful requests only", () => {
    const rows = parseRows([
      row({ first_token_ms: "100", total_ms: "150" }),
      row({ first_token_ms: "200", total_ms: "250" }),
      row({ first_token_ms: "300", total_ms: "350" }),
      row({ status: "error", first_token_ms: "", total_ms: "50", tokens_in: "0", tokens_out: "0" }),
      row({ status: "timeout", first_token_ms: "", total_ms: "20000", tokens_in: "0", tokens_out: "0" }),
      row({ status: "rate_limited", first_token_ms: "", total_ms: "10", tokens_in: "0", tokens_out: "0" }),
    ]);
    const k = kpis(rows);
    expect([k.total, k.ok, k.errors, k.timeouts, k.rateLimited]).toEqual([6, 3, 1, 1, 1]);
    expect(k.avgLatency).toBeCloseTo(250, 9);
    expect(k.p95Latency).toBeCloseTo(340, 9);
    expect(k.ttftP50).toBe(200);
    expect(k.tokens).toBe(360);
    expect(k.okRate).toBeCloseTo(0.5, 9);
  });

  it("has no latency when nothing succeeded (not measured, not zero)", () => {
    const k = kpis(parseRows([row({ status: "error", first_token_ms: "", total_ms: "5" })]));
    expect(k.avgLatency).toBeNull();
    expect(k.p95Latency).toBeNull();
    expect(k.ttftP50).toBeNull();
  });
});

describe("histogram", () => {
  it("puts each value in exactly one bin", () => {
    const h = histogram([5, 50, 250, 999, 1000, 2999, 5000, 15000, 40000, 40000]);
    expect(h.map((b) => b.count)).toEqual([1, 1, 1, 1, 2, 1, 1, 2]);
    expect(h.reduce((s, b) => s + b.count, 0)).toBe(10);
  });
});

describe("throughputSeries", () => {
  it("counts starts and successful completions per bucket from the rows' own times", () => {
    const rows = parseRows([
      row({ offsetS: 0, total_ms: "3000" }),
      row({ offsetS: 5, total_ms: "2000" }),
      row({ offsetS: 12, total_ms: "30000" }),
    ]);
    const s = throughputSeries(rows, 10);
    expect(s.map((b) => b.started)).toEqual([2, 1, 0, 0, 0]);
    expect(s.map((b) => b.completed)).toEqual([2, 0, 0, 0, 1]);
  });
  it("is empty without rows", () => {
    expect(throughputSeries([], 10)).toEqual([]);
  });
});

describe("byScenario", () => {
  it("computes successful requests per minute over the run's own duration and flags back-to-back runs", () => {
    const rows = parseRows([
      row({ offsetS: 0, total_ms: "1000" }),
      row({ offsetS: 30, total_ms: "1000" }),
      row({ scenario: "quality_S2", source: "real", offsetS: 0, total_ms: "1000" }),
    ]);
    const g = Object.fromEntries(byScenario(rows).map((x) => [x.scenario, x]));
    expect(g.steady.durationS).toBeCloseTo(31, 6);
    expect(g.steady.okPerMin).toBeCloseTo((2 / 31) * 60, 6);
    expect(g.steady.closedLoop).toBe(false);
    expect(g.quality_S2.closedLoop).toBe(true);
    expect(g.quality_S2.real).toBe(true);
    expect(isRealScenario("probe_limit")).toBe(true);
  });
});
