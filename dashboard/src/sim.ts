export type Pattern = "steady" | "burst" | "ramp" | "sustained_overload";
export type RetryPolicy = "budgeted" | "naive";

export interface SimSpec {
  pattern: Pattern;
  rate_x: number;
  burst_x: number;
  duration_s: number;
  concurrency: number;
  limits_scale: number;
  deadline_s: number;
  seed: number;
  whatif: { halve_limits_at_s: number | null; slow_p: number; slow_factor: number; retry_policy: RetryPolicy };
}

export interface SimOptions {
  patterns: { id: Pattern; label: string; description: string }[];
  limits: Record<string, any>;
  retry_policies: { id: RetryPolicy; label: string }[];
  capacity_per_min: Record<string, number>;
  est_tokens_per_answer: number;
  max_running: number;
  provenance: string;
}

export interface SimStatus {
  sim_id: string;
  run_id: string;
  scenario: string;
  status: "running" | "finished" | "stopped" | "failed";
  returncode: number | null;
  started_at: string;
  spec: SimSpec;
  log_tail?: string;
  capacity_per_min?: number;
  expected_requests?: number;
}

export const DEFAULT_SPEC: SimSpec = {
  pattern: "steady",
  rate_x: 0.7,
  burst_x: 10,
  duration_s: 90,
  concurrency: 4,
  limits_scale: 5,
  deadline_s: 20,
  seed: 1,
  whatif: { halve_limits_at_s: null, slow_p: 0, slow_factor: 6, retry_policy: "budgeted" },
};

/** Answers per minute the limits allow. It is arithmetic on the documented limits times the scale, not a measurement.
 *  Both the request and the token limit scale linearly, so the capacity does too. */
export function capacityPerMin(options: SimOptions | null, scale: number): number | null {
  const one = options?.capacity_per_min["1"];
  return one === undefined ? null : one * scale;
}

/** The requests the chosen load offers: the same phase arithmetic the backend uses to build the scenario. */
export function expectedRequests(spec: SimSpec, capPerMin: number | null): number | null {
  if (capPerMin === null) return null;
  const d = spec.duration_s;
  let rateSeconds: number;
  if (spec.pattern === "burst") {
    const calm1 = Math.round(d * 0.4);
    const burst = Math.max(10, Math.round(d * 0.15));
    const calm2 = d - calm1 - burst;
    rateSeconds = calm1 * spec.rate_x + burst * spec.burst_x + calm2 * spec.rate_x;
  } else if (spec.pattern === "ramp") {
    rateSeconds = d * spec.rate_x * 0.6; // five equal steps at 1/5 .. 5/5 of the rate
  } else {
    rateSeconds = d * spec.rate_x;
  }
  return Math.round((rateSeconds * capPerMin) / 60);
}

/** What the form must satisfy before the server will even look at it (the server checks again). */
export function validate(spec: SimSpec): string | null {
  if (spec.pattern === "sustained_overload" && spec.rate_x < 1.5) return "Sustained overload needs a rate of at least 1.5 times capacity.";
  if (spec.pattern === "burst" && spec.duration_s < 40) return "A burst needs a run of at least 40 seconds.";
  if (spec.whatif.halve_limits_at_s !== null && spec.whatif.halve_limits_at_s >= spec.duration_s) return "The limits would be halved after the run ends; pick an earlier time.";
  return null;
}

export interface PairKind {
  id: string;
  label: string;
  explain: string;
  a: (s: SimSpec) => SimSpec;
  b: (s: SimSpec) => SimSpec;
  names: [string, string];
}

const clone = (s: SimSpec): SimSpec => ({ ...s, whatif: { ...s.whatif } });

/** Two runs that differ in exactly one thing and share the seed, so the same arrival times hit both. */
export const PAIRS: PairKind[] = [
  {
    id: "retry",
    label: "Naive against budgeted retries",
    explain: "Same load and seed. The naive run has no rate limiter, immediate retries and no concurrency cap (as in the retry_naive scenario), so it ignores the concurrency setting.",
    names: ["Budgeted retries", "Naive retries"],
    a: (s) => ({ ...clone(s), whatif: { ...s.whatif, retry_policy: "budgeted" } }),
    b: (s) => ({ ...clone(s), whatif: { ...s.whatif, retry_policy: "naive" } }),
  },
  {
    id: "load",
    label: "This load against double the load",
    explain: "Same pattern and seed; the second run offers twice the rate (up to 4 times capacity).",
    names: ["As set", "Double the rate"],
    a: (s) => clone(s),
    b: (s) => ({ ...clone(s), rate_x: Math.min(4, Math.round(s.rate_x * 2 * 100) / 100) }),
  },
  {
    id: "limits",
    label: "Normal limits against limits halved mid-run",
    explain: "The stand-in halves its RPM and TPM halfway through the second run without telling the client; the gateway has to notice.",
    names: ["Limits unchanged", "Halved at 50%"],
    a: (s) => ({ ...clone(s), whatif: { ...s.whatif, halve_limits_at_s: null } }),
    b: (s) => ({ ...clone(s), whatif: { ...s.whatif, halve_limits_at_s: Math.max(10, Math.round(s.duration_s / 2)) } }),
  },
  {
    id: "slow",
    label: "Fast replies against slow replies",
    explain: "The second run makes 15% of stand-in replies succeed 6 times slower (a choice, not a measurement).",
    names: ["No slow replies", "15% slow, 6x"],
    a: (s) => ({ ...clone(s), whatif: { ...s.whatif, slow_p: 0 } }),
    b: (s) => ({ ...clone(s), whatif: { ...s.whatif, slow_p: 0.15, slow_factor: 6 } }),
  },
];

function formatDetail(detail: unknown, status: number): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d: any) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg}`).join("; ");
  return `request failed (${status})`;
}

async function send<T>(url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await r.text();
  let json: any = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    /* not JSON */
  }
  if (!r.ok) throw new Error(formatDetail(json?.detail, r.status));
  return json as T;
}

export const startSim = (spec: SimSpec) => send<SimStatus>("/api/simulations", spec);
export const stopSim = (id: string) => send<SimStatus>(`/api/simulations/${id}/stop`);
