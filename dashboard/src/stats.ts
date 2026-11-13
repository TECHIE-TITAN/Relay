import type { RequestRow } from "./types";

/** Linear interpolation between closest ranks (type 7): the same definition the Python side uses. */
export function percentile(values: number[], q: number): number | null {
  if (values.length === 0) return null;
  const xs = [...values].sort((a, b) => a - b);
  if (xs.length === 1) return xs[0];
  const pos = ((xs.length - 1) * q) / 100;
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo);
}

export function fmtMs(v: number | null | undefined): string {
  if (v === null || v === undefined) return "not measured";
  if (v >= 10000) return `${(v / 1000).toFixed(1)} s`;
  if (v >= 1000) return `${(v / 1000).toFixed(2)} s`;
  return `${v.toFixed(v < 10 ? 1 : 0)} ms`;
}

export function fmtNum(v: number | null | undefined, digits = 1): string {
  return v === null || v === undefined ? "not measured" : v.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

export function fmtPct(count: number, total: number): string {
  return total ? `${((count / total) * 100).toFixed(1)}%` : "not measured";
}

export interface RunSummary {
  n: number;
  ok: number;
  byStatus: Record<string, number>;
  ft: { n: number; p50: number | null; p95: number | null };
  total: { p50: number | null; p95: number | null };
  retriesPerRequest: number | null;
  tokensPerAnswer: number | null;
  modes: Record<string, number>;
}

export function summarizeRows(rows: RequestRow[]): RunSummary {
  const ok = rows.filter((r) => r.status === "ok");
  const ft = ok.map((r) => r.first_token_ms).filter((v): v is number => v !== null);
  const tt = ok.map((r) => r.total_ms);
  const byStatus: Record<string, number> = {};
  const modes: Record<string, number> = {};
  for (const r of rows) byStatus[r.status] = (byStatus[r.status] ?? 0) + 1;
  for (const r of ok) modes[r.answer_mode ?? "unknown"] = (modes[r.answer_mode ?? "unknown"] ?? 0) + 1;
  const tokens = ok.reduce((s, r) => s + r.tokens_in + r.tokens_out, 0);
  return {
    n: rows.length,
    ok: ok.length,
    byStatus,
    ft: { n: ft.length, p50: percentile(ft, 50), p95: percentile(ft, 95) },
    total: { p50: percentile(tt, 50), p95: percentile(tt, 95) },
    retriesPerRequest: rows.length ? rows.reduce((s, r) => s + r.retries, 0) / rows.length : null,
    tokensPerAnswer: ok.length ? tokens / ok.length : null,
    modes,
  };
}

/** Scenario names that ran on the real service (everything else is the stand-in). */
export function isRealScenario(name: string): boolean {
  return name.startsWith("quality_") || name.startsWith("probe_");
}

export const PROVENANCE: Record<string, { label: string; style: "solid" | "filled" | "dashed" }> = {
  DOCUMENTED: { label: "Documented", style: "solid" },
  "OBSERVED-REPORTED": { label: "Observed (reported)", style: "filled" },
  "OBSERVED-TRIGGERED": { label: "Observed (triggered)", style: "filled" },
  SIMULATED: { label: "Simulated", style: "dashed" },
};

export function provenanceKey(text: string | null | undefined): string | null {
  if (!text) return null;
  for (const k of Object.keys(PROVENANCE)) if (text.toUpperCase().startsWith(k)) return k;
  return null;
}
