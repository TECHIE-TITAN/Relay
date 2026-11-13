import { describe, expect, it } from "vitest";
import { DEFAULT_SPEC, PAIRS, capacityPerMin, expectedRequests, validate } from "./sim";
import type { SimOptions, SimSpec } from "./sim";

const options = { capacity_per_min: { "1": 3.64, "5": 18.18 } } as unknown as SimOptions;
const spec = (over: Partial<SimSpec> = {}): SimSpec => ({ ...DEFAULT_SPEC, whatif: { ...DEFAULT_SPEC.whatif }, ...over });

describe("capacity and expected load", () => {
  it("scales the capacity with the limits scale and says nothing when options are missing", () => {
    expect(capacityPerMin(options, 5)).toBeCloseTo(18.2, 6);
    expect(capacityPerMin(null, 5)).toBeNull();
    expect(expectedRequests(spec(), null)).toBeNull();
  });
  it("matches the phase arithmetic the backend uses", () => {
    const cap = 18.2; // answers per minute
    expect(expectedRequests(spec({ pattern: "steady", rate_x: 0.7, duration_s: 90 }), cap)).toBe(Math.round((90 * 0.7 * cap) / 60));
    expect(expectedRequests(spec({ pattern: "ramp", rate_x: 1, duration_s: 100 }), cap)).toBe(Math.round((100 * 0.6 * cap) / 60));
    // burst, 100 s: 40 s calm + 15 s burst + 45 s calm
    expect(expectedRequests(spec({ pattern: "burst", rate_x: 0.5, burst_x: 10, duration_s: 100 }), cap)).toBe(Math.round(((40 * 0.5 + 15 * 10 + 45 * 0.5) * cap) / 60));
  });
});

describe("validation mirrors the server", () => {
  it("rejects what the server would reject", () => {
    expect(validate(spec())).toBeNull();
    expect(validate(spec({ pattern: "sustained_overload", rate_x: 1 }))).toMatch(/1.5/);
    expect(validate(spec({ pattern: "burst", duration_s: 30 }))).toMatch(/40 seconds/);
    expect(validate(spec({ whatif: { ...DEFAULT_SPEC.whatif, halve_limits_at_s: 120 }, duration_s: 90 }))).toMatch(/after the run ends/);
  });
});

describe("comparison pairs differ in exactly one thing", () => {
  const base = spec({ rate_x: 1.0, seed: 7, duration_s: 100 });
  it("keeps the seed and the load pattern and changes only the named variable", () => {
    for (const p of PAIRS) {
      const a = p.a(base);
      const b = p.b(base);
      expect(a.seed).toBe(7);
      expect(b.seed).toBe(7);
      expect(a.pattern).toBe(b.pattern);
      expect(JSON.stringify(a)).not.toBe(JSON.stringify(b));
    }
  });
  it("retry pair: budgeted against naive", () => {
    const p = PAIRS.find((x) => x.id === "retry")!;
    expect([p.a(base).whatif.retry_policy, p.b(base).whatif.retry_policy]).toEqual(["budgeted", "naive"]);
    expect(p.a(base).rate_x).toBe(p.b(base).rate_x);
  });
  it("load pair doubles the rate but never above 4x capacity", () => {
    const p = PAIRS.find((x) => x.id === "load")!;
    expect(p.b(base).rate_x).toBe(2);
    expect(p.b(spec({ rate_x: 3 })).rate_x).toBe(4);
  });
  it("limits pair halves at the middle of the run", () => {
    const p = PAIRS.find((x) => x.id === "limits")!;
    expect([p.a(base).whatif.halve_limits_at_s, p.b(base).whatif.halve_limits_at_s]).toEqual([null, 50]);
  });
  it("does not change the form's own spec", () => {
    const before = JSON.stringify(base);
    PAIRS.forEach((p) => {
      p.a(base);
      p.b(base);
    });
    expect(JSON.stringify(base)).toBe(before);
  });
});
