import type { AggRow } from "./types";
import { isRealScenario, percentile } from "./stats";

/** Rows from /api/rows with numbers parsed. A blank stays null: nothing is turned into a zero. */
export interface Row {
  run_id: string;
  scenario: string;
  source: string;
  start: number; // epoch ms
  ft: number | null;
  total: number | null;
  status: string;
  retries: number;
  tin: number;
  tout: number;
}

const num = (s: string | null | undefined): number | null => (s === null || s === undefined || s === "" || Number.isNaN(Number(s)) ? null : Number(s));

export function parseRows(rows: AggRow[]): Row[] {
  return rows.map((r) => ({
    run_id: r.run_id,
    scenario: r.scenario,
    source: r.source,
    start: Date.parse(r.start_time),
    ft: num(r.first_token_ms),
    total: num(r.total_ms),
    status: r.status,
    retries: num(r.retries) ?? 0,
    tin: num(r.tokens_in) ?? 0,
    tout: num(r.tokens_out) ?? 0,
  }));
}

export interface Kpis {
  total: number;
  ok: number;
  errors: number;
  timeouts: number;
  rateLimited: number;
  avgLatency: number | null;
  p95Latency: number | null;
  ttftP50: number | null;
  ttftP95: number | null;
  tokens: number;
  okRate: number | null;
}

export function kpis(rows: Row[]): Kpis {
  const by = (s: string) => rows.filter((r) => r.status === s).length;
  const ok = rows.filter((r) => r.status === "ok");
  const tt = ok.map((r) => r.total).filter((v): v is number => v !== null);
  const ft = ok.map((r) => r.ft).filter((v): v is number => v !== null);
  return {
    total: rows.length,
    ok: ok.length,
    errors: by("error"),
    timeouts: by("timeout"),
    rateLimited: by("rate_limited"),
    avgLatency: tt.length ? tt.reduce((s, v) => s + v, 0) / tt.length : null,
    p95Latency: percentile(tt, 95),
    ttftP50: percentile(ft, 50),
    ttftP95: percentile(ft, 95),
    tokens: rows.reduce((s, r) => s + r.tin + r.tout, 0),
    okRate: rows.length ? ok.length / rows.length : null,
  };
}

export const BINS: { label: string; lo: number; hi: number }[] = [
  { label: "under 10 ms", lo: 0, hi: 10 },
  { label: "10 to 100 ms", lo: 10, hi: 100 },
  { label: "100 to 300 ms", lo: 100, hi: 300 },
  { label: "300 ms to 1 s", lo: 300, hi: 1000 },
  { label: "1 to 3 s", lo: 1000, hi: 3000 },
  { label: "3 to 10 s", lo: 3000, hi: 10000 },
  { label: "10 to 30 s", lo: 10000, hi: 30000 },
  { label: "over 30 s", lo: 30000, hi: Infinity },
];

export function histogram(values: number[]): { label: string; count: number }[] {
  return BINS.map((b) => ({ label: b.label, count: values.filter((v) => v >= b.lo && v < b.hi).length }));
}

export interface ScenarioAgg {
  scenario: string;
  real: boolean;
  n: number;
  ok: number;
  rateLimited: number;
  timeout: number;
  error: number;
  tokensIn: number;
  tokensOut: number;
  okPerMin: number | null;
  durationS: number | null;
  closedLoop: boolean;
}

export function byScenario(rows: Row[]): ScenarioAgg[] {
  const groups = new Map<string, Row[]>();
  for (const r of rows) groups.set(r.scenario, [...(groups.get(r.scenario) ?? []), r]);
  return Array.from(groups.entries()).map(([scenario, rs]) => {
    const starts = rs.map((r) => r.start).filter((v) => !Number.isNaN(v));
    const ends = rs.map((r) => r.start + (r.total ?? 0)).filter((v) => !Number.isNaN(v));
    const dur = starts.length ? (Math.max(...ends) - Math.min(...starts)) / 1000 : null;
    const ok = rs.filter((r) => r.status === "ok").length;
    return {
      scenario,
      real: isRealScenario(scenario),
      n: rs.length,
      ok,
      rateLimited: rs.filter((r) => r.status === "rate_limited").length,
      timeout: rs.filter((r) => r.status === "timeout").length,
      error: rs.filter((r) => r.status === "error").length,
      tokensIn: rs.reduce((s, r) => s + r.tin, 0),
      tokensOut: rs.reduce((s, r) => s + r.tout, 0),
      okPerMin: dur && dur > 0 ? (ok / dur) * 60 : null,
      durationS: dur,
      closedLoop: scenario.startsWith("quality_") || scenario.startsWith("probe_"),
    };
  });
}

/** Requests started and completed per bucket, for one scenario, from the rows' own start times and total times. */
export function throughputSeries(rows: Row[], bucketS = 10): { t: number; started: number; completed: number }[] {
  const valid = rows.filter((r) => !Number.isNaN(r.start));
  if (valid.length === 0) return [];
  const t0 = Math.min(...valid.map((r) => r.start));
  const end = Math.max(...valid.map((r) => r.start + (r.total ?? 0)));
  const n = Math.max(1, Math.ceil((end - t0) / 1000 / bucketS));
  const out = Array.from({ length: n }, (_, i) => ({ t: i * bucketS, started: 0, completed: 0 }));
  for (const r of valid) {
    const s = Math.min(n - 1, Math.floor((r.start - t0) / 1000 / bucketS));
    out[s].started += 1;
    if (r.status === "ok") {
      const c = Math.min(n - 1, Math.floor((r.start + (r.total ?? 0) - t0) / 1000 / bucketS));
      out[c].completed += 1;
    }
  }
  return out;
}
