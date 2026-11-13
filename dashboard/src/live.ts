import { percentile } from "./stats";
import type { GatewaySample, TraceEvent } from "./types";

/** Turns the runner's own event stream into what the Live Simulation page shows. Nothing is simulated here:
 *  every request state, count and chart value is derived from an event the pipeline logged, and a stage a
 *  request has not reached yet is simply not shown. */

export type LiveState = "queued" | "processing" | "retrying" | "successful" | "rejected" | "failed";
export type NodeId = "admission" | "retrieve" | "gateway" | "model" | "verify" | "release";

export const NODES: { id: NodeId; label: string; hint: string }[] = [
  { id: "admission", label: "Admission queue", hint: "Waiting at the door for a free slot (bounded, first in first out)" },
  { id: "retrieve", label: "Retrieve and decide", hint: "Searching the papers and choosing to answer or refuse" },
  { id: "gateway", label: "Gateway and rate limiter", hint: "Reserving tokens in the 60 s window; waiting for the limiter; retry back-off" },
  { id: "model", label: "Stand-in model", hint: "The call to the stand-in model server is in flight" },
  { id: "verify", label: "Verification", hint: "The local checks on the drafted answer" },
  { id: "release", label: "Response release", hint: "The checked answer is being released" },
];

export interface LogLine {
  t: number;
  event: string;
  note: string;
}

export interface LiveReq {
  id: string;
  qid: string;
  node: NodeId;
  state: LiveState;
  arrive: number;
  attempts: number;
  retryEvents: number;
  regenerated: boolean;
  done: boolean;
  status?: string;
  mode?: string | null;
  shed?: string | null;
  total?: number | null;
  firstToken?: number | null;
  events: LogLine[];
}

export interface Bucket {
  i: number;
  arrived: number;
  completed: number;
  ok: number;
  retries: number;
  limit: number;
  totals: number[];
}

export interface DoneRec {
  status: string;
  total: number | null;
  retries: number;
  tin: number;
  tout: number;
  shed: string | null;
}

const NOTE_KEYS = ["purpose", "attempt", "ok", "error_kind", "status_code", "duration_ms", "wait_ms", "waited_ms", "sleep_ms", "reason", "branch", "passed", "failures", "mode", "status", "answer_mode", "level", "shed_reason", "ms"];
const SKIP_IN_LOG = new Set(["launch", "last_token", "slot_released"]);
const NON_RUN_EVENTS = new Set(["gateway_state", "run_start", "run_end"]);

function note(e: TraceEvent): string {
  return NOTE_KEYS.filter((k) => e[k] !== undefined && e[k] !== null && e[k] !== "")
    .map((k) => `${k}=${typeof e[k] === "object" ? JSON.stringify(e[k]) : String(e[k])}`)
    .join(", ")
    .slice(0, 200);
}

export class LiveModel {
  reqs = new Map<string, LiveReq>();
  samples: GatewaySample[] = [];
  runLog: LogLine[] = [];
  limitLog: LogLine[] = [];
  dones: DoneRec[] = [];
  arrivals: number[] = [];
  buckets = new Map<number, Bucket>();
  bucketS: number;
  t0: number | null = null;
  lastT = 0;
  ended = false;
  interrupted = false;
  eventCount = 0;
  provider429 = 0;
  breakerOpens = 0; // times the circuit breaker opened (a request-level event in the log)
  pauses = 0; // global pauses the gateway started (after a 429 or when the breaker opened)

  constructor(bucketS = 5) {
    this.bucketS = bucketS;
  }

  private bucket(t: number): Bucket {
    const i = Math.max(0, Math.floor((t - (this.t0 ?? 0)) / this.bucketS));
    let b = this.buckets.get(i);
    if (!b) {
      b = { i, arrived: 0, completed: 0, ok: 0, retries: 0, limit: 0, totals: [] };
      this.buckets.set(i, b);
    }
    return b;
  }

  ingest(e: TraceEvent): void {
    const t = Number(e.t_mono ?? 0);
    if (this.t0 === null) this.t0 = t;
    this.lastT = Math.max(this.lastT, t);
    this.eventCount += 1;

    if (e.request_id === null || e.request_id === undefined) {
      if (e.event === "gateway_state") {
        this.samples.push(e as unknown as GatewaySample);
        if (this.samples.length > 4000) this.samples.splice(0, this.samples.length - 4000);
      } else if (!NON_RUN_EVENTS.has(e.event)) {
        const line = { t, event: e.event, note: note(e) };
        if (e.event === "pause_start") this.pauses += 1;
        this.runLog.push(line);
        if (["pause_start", "limit_changed", "daily_limit", "breaker_open", "control"].includes(e.event)) {
          this.limitLog.push(line);
          if (e.event !== "control") this.bucket(t).limit += 1;
        }
        if (this.runLog.length > 600) this.runLog.shift();
        if (this.limitLog.length > 300) this.limitLog.shift();
      }
      return;
    }

    const id = String(e.request_id);
    let r = this.reqs.get(id);
    if (!r) {
      r = { id, qid: "", node: "admission", state: "queued", arrive: t, attempts: 0, retryEvents: 0, regenerated: false, done: false, events: [] };
      this.reqs.set(id, r);
    }
    if (!SKIP_IN_LOG.has(e.event) && r.events.length < 80) r.events.push({ t, event: e.event, note: note(e) });

    switch (e.event) {
      case "arrive":
        r.arrive = t;
        r.qid = String(e.question_id ?? "");
        r.node = "admission";
        r.state = "queued";
        this.arrivals.push(t);
        this.bucket(t).arrived += 1;
        break;
      case "shed":
        r.shed = String(e.reason ?? "");
        break;
      case "slot_acquired":
        r.node = "retrieve";
        r.state = "processing";
        break;
      case "limiter_wait_start":
        r.node = "gateway";
        r.state = "queued";
        break;
      case "limiter_wait_end":
        r.node = "gateway";
        r.state = r.attempts > 0 ? "retrying" : "processing";
        break;
      case "send":
        r.node = "model";
        r.attempts += 1;
        r.state = Number(e.attempt ?? 1) > 1 || e.purpose === "regenerate" ? "retrying" : "processing";
        break;
      case "call_end":
        if (e.ok) {
          r.node = "verify";
          r.state = "processing";
        } else {
          r.node = "gateway";
          r.state = "retrying";
          if (String(e.error_kind ?? "").startsWith("rate_limit")) {
            this.bucket(t).limit += 1;
            this.provider429 += 1;
            const line = { t, event: "provider 429", note: `${r.qid || r.id.slice(-4)}: ${String(e.error_kind)}${e.retry_after_s ? `, retry-after ${e.retry_after_s}s` : ""}` };
            this.limitLog.push(line);
            if (this.limitLog.length > 300) this.limitLog.shift();
          }
        }
        break;
      case "breaker_open": {
        this.breakerOpens += 1;
        this.bucket(t).limit += 1;
        this.limitLog.push({ t, event: "circuit breaker opened", note: `after repeated failures; cooldown ${e.cooldown_s ?? "?"} s (seen by ${r.qid || r.id.slice(-4)})` });
        if (this.limitLog.length > 300) this.limitLog.shift();
        break;
      }
      case "daily_limit": {
        this.bucket(t).limit += 1;
        this.limitLog.push({ t, event: "daily limit reached", note: note(e) });
        if (this.limitLog.length > 300) this.limitLog.shift();
        break;
      }
      case "retry_sleep_start":
        r.node = "gateway";
        r.state = "retrying";
        r.retryEvents += 1;
        this.bucket(t).retries += 1;
        break;
      case "loop_regenerate":
        r.node = "gateway";
        r.state = "retrying";
        r.regenerated = true;
        r.retryEvents += 1;
        this.bucket(t).retries += 1;
        break;
      case "verify_result":
        r.node = "verify";
        r.state = "processing";
        break;
      case "first_token":
        r.node = "release";
        r.state = "processing";
        r.mode = String(e.mode ?? "");
        r.firstToken = Number(e.ms ?? 0);
        break;
      case "done": {
        r.done = true;
        r.status = String(e.status ?? "");
        r.mode = (e.answer_mode as string | null | undefined) ?? r.mode ?? null;
        r.shed = (e.shed_reason as string | null | undefined) ?? r.shed ?? null;
        r.total = e.total_ms === undefined || e.total_ms === null ? null : Number(e.total_ms);
        r.node = "release";
        r.state = r.status === "ok" ? "successful" : r.shed || r.status === "rate_limited" ? "rejected" : "failed";
        const b = this.bucket(t);
        b.completed += 1;
        if (r.status === "ok") b.ok += 1;
        if (r.total !== null && r.total !== undefined && r.status === "ok") b.totals.push(r.total);
        this.dones.push({ status: r.status, total: r.total ?? null, retries: Number(e.retries ?? 0), tin: Number(e.tokens_in ?? 0), tout: Number(e.tokens_out ?? 0), shed: r.shed ?? null });
        break;
      }
      default:
        break;
    }
  }

  /** Requests currently in each stage, by state, and the finished tallies. */
  counts(): { nodes: Record<NodeId, Record<LiveState, number>>; finished: { successful: number; rejected: number; failed: number }; arrived: number; open: number } {
    const blank = (): Record<LiveState, number> => ({ queued: 0, processing: 0, retrying: 0, successful: 0, rejected: 0, failed: 0 });
    const nodes = { admission: blank(), retrieve: blank(), gateway: blank(), model: blank(), verify: blank(), release: blank() } as Record<NodeId, Record<LiveState, number>>;
    const finished = { successful: 0, rejected: 0, failed: 0 };
    let open = 0;
    this.reqs.forEach((r) => {
      if (r.done) {
        if (r.state === "successful" || r.state === "rejected" || r.state === "failed") finished[r.state] += 1;
      } else {
        nodes[r.node][r.state] += 1;
        open += 1;
      }
    });
    return { nodes, finished, arrived: this.reqs.size, open };
  }

  /** Requests in a stage, oldest first. */
  inNode(node: NodeId): LiveReq[] {
    const out: LiveReq[] = [];
    this.reqs.forEach((r) => {
      if (!r.done && r.node === node) out.push(r);
    });
    return out.sort((a, b) => a.arrive - b.arrive);
  }

  recentFinished(n = 30): LiveReq[] {
    const out: LiveReq[] = [];
    this.reqs.forEach((r) => {
      if (r.done) out.push(r);
    });
    return out.sort((a, b) => b.arrive - a.arrive).slice(0, n);
  }

  arrivalsPerMin(windowS = 30): number | null {
    if (this.arrivals.length === 0 || this.t0 === null) return null;
    const elapsed = Math.min(windowS, Math.max(1, this.lastT - this.t0));
    const n = this.arrivals.filter((t) => t >= this.lastT - windowS).length;
    return (n / elapsed) * 60;
  }

  series(): { t: number; arrived: number; completed: number; retries: number; limit: number; p50: number | null; p95: number | null }[] {
    if (this.buckets.size === 0) return [];
    const last = Math.max(...Array.from(this.buckets.keys()));
    const out = [];
    for (let i = 0; i <= last; i++) {
      const b = this.buckets.get(i);
      out.push({
        t: i * this.bucketS,
        arrived: b?.arrived ?? 0,
        completed: b?.completed ?? 0,
        retries: b?.retries ?? 0,
        limit: b?.limit ?? 0,
        p50: b && b.totals.length ? percentile(b.totals, 50) : null,
        p95: b && b.totals.length ? percentile(b.totals, 95) : null,
      });
    }
    return out;
  }

  /** What the comparison table shows for one run, from the done events. */
  summary(): {
    n: number;
    ok: number;
    rejected: number;
    failed: number;
    okRate: number | null;
    p50: number | null;
    p95: number | null;
    retriesPerRequest: number | null;
    tokens: number;
    peakQueue: number | null;
    peakInflight: number | null;
    provider429: number;
    regenerated: number;
    breakerOpens: number;
    pauses: number;
  } {
    const ok = this.dones.filter((d) => d.status === "ok");
    const totals = ok.map((d) => d.total).filter((v): v is number => v !== null);
    const rejected = this.dones.filter((d) => d.status !== "ok" && (d.shed || d.status === "rate_limited")).length;
    const q = this.samples.map((s) => s.queue_len).filter((v): v is number => typeof v === "number");
    const f = this.samples.map((s) => s.inflight).filter((v): v is number => typeof v === "number");
    let regenerated = 0;
    this.reqs.forEach((r) => {
      if (r.regenerated) regenerated += 1;
    });
    return {
      n: this.dones.length,
      ok: ok.length,
      rejected,
      failed: this.dones.length - ok.length - rejected,
      okRate: this.dones.length ? ok.length / this.dones.length : null,
      p50: percentile(totals, 50),
      p95: percentile(totals, 95),
      retriesPerRequest: this.dones.length ? this.dones.reduce((s, d) => s + d.retries, 0) / this.dones.length : null,
      tokens: this.dones.reduce((s, d) => s + d.tin + d.tout, 0),
      peakQueue: q.length ? Math.max(...q) : null,
      peakInflight: f.length ? Math.max(...f) : null,
      provider429: this.provider429,
      regenerated,
      breakerOpens: this.breakerOpens,
      pauses: this.pauses,
    };
  }
}
