// Shapes returned by the read-only API (src/relay/dashboard_api.py). Manifests are loosely typed on purpose:
// the dashboard shows what is logged and never assumes a field exists.

export interface RunInfo {
  run_id: string;
  scenario: string;
  source: string;
  kind?: string | null;
  wall_start_utc?: string | null;
  wall_end_utc?: string | null;
  rows: number | null;
  live: boolean;
  interrupted?: boolean;
  origin?: "evidence" | "simulation" | string;
  incomplete: boolean;
  graded: boolean;
  provenance?: string | null;
}

export interface Invariant {
  id: string;
  description?: string;
  applicable: boolean;
  passed: boolean;
  checked?: number;
}

export interface RunDetail {
  run_id: string;
  scenario: string;
  description?: string;
  source: string;
  provenance?: string;
  kind?: string;
  wall_start_utc?: string;
  wall_end_utc?: string;
  limits?: { documented?: Record<string, number>; used?: Record<string, number>; scale?: number; used_provenance?: string };
  summary?: Record<string, any>;
  invariants?: Invariant[];
  incomplete?: boolean;
  stop?: Record<string, any> | null;
  grading?: Record<string, any> | null;
  gateway_config?: Record<string, any>;
  pipeline_config?: Record<string, any>;
  note?: string;
}

export interface RequestRow {
  request_id: string;
  question_id?: string | null;
  start_time: string;
  first_token_ms: number | null;
  total_ms: number;
  status: "ok" | "rate_limited" | "timeout" | "error" | string;
  retries: number;
  tokens_in: number;
  tokens_out: number;
  retrieval_setting?: string | null;
  answer_mode?: string | null;
  fail_reason?: string | null;
  shed_reason?: string | null;
  ladder_level?: number | null;
  regenerated?: boolean;
  v0_passed?: boolean | null;
  model_first_token_ms?: number | null;
  arrive_t: number;
  waiting_ms: number;
  working_ms: number;
  retrying_ms: number;
  unaccounted_ms: number;
}

export interface GatewaySample {
  t_mono: number;
  tpm_used?: number;
  tpm_limit?: number;
  rpm_used?: number;
  rpm_limit?: number;
  inflight?: number;
  queue_len?: number;
  calls_in_flight?: number;
  paused?: boolean;
  aimd_factor?: number;
  daily_tokens?: number;
  daily_token_cap?: number;
  daily_exhausted?: boolean;
}

export interface TraceEvent {
  event: string;
  t_mono: number;
  request_id?: string | null;
  [k: string]: any;
}

export interface TraceResponse {
  request_id: string;
  row: RequestRow | null;
  events: TraceEvent[];
}

/** One request row from /api/rows (values come from results.csv, so numbers are strings and blanks are ""). */
export interface AggRow {
  run_id: string;
  scenario: string;
  source: string;
  start_time: string;
  first_token_ms: string | null;
  total_ms: string | null;
  status: string;
  retries: string | null;
  tokens_in: string | null;
  tokens_out: string | null;
  retrieval_setting?: string | null;
  answer_grade?: string | null;
}

export interface RunMeta {
  scenario: string;
  source: string;
  kind?: string | null;
  wall_start_utc?: string | null;
  peak_queue_len?: number | null;
  peak_inflight?: number | null;
  incomplete: boolean;
}

export interface RowsResponse {
  count: number;
  runs: string[];
  run_meta: Record<string, RunMeta>;
  rows: AggRow[];
}

export interface Dist {
  n: number;
  p50: number | null;
  p95: number | null;
  p99: number | null;
}

export interface ScenarioStats {
  n: number;
  duration_s: number | null;
  offered_per_min: number | null;
  by_status: Record<string, number>;
  retries_per_request: number | null;
  tokens_per_answer: number | null;
  tokens_per_model_answer: number | null;
  cost_per_1000: number | null;
  n_graded: number;
  grades: Record<string, number>;
  latency: { n_ok: number; ft: Dist; total: Dist };
}

export interface RetrievalScore {
  asked: number;
  answerable: number;
  answerable_abstained: number;
  unanswerable_reach_model: number;
  recall_sum: number;
  hit_all: number;
  ctx: number;
  reach: number;
}

export interface Experiments {
  available: boolean;
  reason?: string;
  rows?: number;
  scenarios?: Record<string, ScenarioStats>;
  model_ttft?: Record<string, { n: number; p50: number | null; p95: number | null }>;
  grade_detail?: Record<string, Record<string, number>>;
  retrieval?: { file: string; settings: Record<string, RetrievalScore> } | null;
  settings?: Record<string, { chunk_tokens?: number; overlap_tokens?: number; k?: number; fetch_k?: number; rerank?: string; section_aware?: boolean }>;
  sources?: {
    generated_at?: string;
    rows?: number;
    runs_used?: { scenario: string; run_id: string; rows: number; incomplete: boolean; graded: boolean }[];
    runs_left_out?: { run_id: string; scenario: string; reason: string }[];
  } | null;
}

export interface Limits {
  documented: Record<string, any>;
  observed_reported: Record<string, any> | null;
  observed_triggered: Record<string, any> | null;
  probe_calls_by_stage: Record<string, number>;
  simulated: Record<string, any>;
  daily_limit_note: string;
}

export interface Ledger {
  available: boolean;
  file?: string;
  date?: string;
  tokens?: number;
  requests?: number;
  token_cap?: number;
  request_cap?: number;
  provider_daily_limit_hit?: boolean;
}
