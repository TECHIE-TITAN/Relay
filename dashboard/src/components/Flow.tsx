import type { ReactNode } from "react";
import { LiveModel, LiveReq, LiveState, NODES, NodeId } from "../live";

/** Six request states, each a colour AND a shape, so the pipeline reads without colour. */
export const STATE_META: Record<LiveState, { label: string; color: string; hint: string }> = {
  processing: { label: "Processing", color: "var(--c1)", hint: "being worked on" },
  queued: { label: "Queued", color: "var(--c2)", hint: "waiting for the queue or the rate limiter" },
  retrying: { label: "Retrying", color: "var(--c3)", hint: "a failed call being retried, or an answer being regenerated" },
  successful: { label: "Successful", color: "var(--st-ok)", hint: "an answer was released" },
  rejected: { label: "Rejected", color: "var(--st-rate)", hint: "shed at the door or limited (rate-limited, daily budget)" },
  failed: { label: "Failed", color: "var(--st-error)", hint: "timed out or errored" },
};

export function Glyph({ state, size = 12 }: { state: LiveState; size?: number }) {
  const c = STATE_META[state].color;
  const s = size;
  const h = s / 2;
  return (
    <svg width={s} height={s} viewBox={`0 0 ${s} ${s}`} aria-hidden focusable="false">
      {state === "processing" && <circle cx={h} cy={h} r={h - 1} fill={c} />}
      {state === "queued" && <circle cx={h} cy={h} r={h - 1.5} fill="none" stroke={c} strokeWidth={2} />}
      {state === "retrying" && <rect x={h - h * 0.62} y={h - h * 0.62} width={h * 1.24} height={h * 1.24} fill={c} transform={`rotate(45 ${h} ${h})`} />}
      {state === "successful" && <path d={`M${s * 0.18} ${s * 0.55} L${s * 0.42} ${s * 0.78} L${s * 0.84} ${s * 0.25}`} fill="none" stroke={c} strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round" />}
      {state === "rejected" && <rect x={1.5} y={1.5} width={s - 3} height={s - 3} fill={c} stroke="var(--ink2)" strokeWidth={0.8} />}
      {state === "failed" && <path d={`M${s * 0.2} ${s * 0.2} L${s * 0.8} ${s * 0.8} M${s * 0.8} ${s * 0.2} L${s * 0.2} ${s * 0.8}`} stroke={c} strokeWidth={2.2} strokeLinecap="round" />}
    </svg>
  );
}

export function StateLegend() {
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-[13px] text-ink2" aria-label="Request states">
      {(Object.keys(STATE_META) as LiveState[]).map((s) => (
        <li key={s} className="flex items-center gap-1.5" title={STATE_META[s].hint}>
          <Glyph state={s} />
          {STATE_META[s].label}
        </li>
      ))}
    </ul>
  );
}

function Dot({ r, selected, onPick }: { r: LiveReq; selected: boolean; onPick: (id: string) => void }) {
  return (
    <button
      onClick={() => onPick(r.id)}
      title={`${r.qid || r.id.slice(-4)}: ${STATE_META[r.state].label}${r.status ? `, ${r.status}` : ""}`}
      aria-label={`${r.qid || r.id}: ${STATE_META[r.state].label}. Inspect`}
      className={`inline-flex h-[18px] w-[18px] items-center justify-center rounded-sm ${selected ? "bg-grid outline outline-1 outline-ink" : "hover:bg-grid"}`}
    >
      <Glyph state={r.state} />
    </button>
  );
}

const MAX_DOTS = 42;

function NodeBox({ title, hint, count, children, extra }: { title: string; hint: string; count: number | string; children?: ReactNode; extra?: ReactNode }) {
  return (
    <div className="min-w-0 flex-1 basis-[9rem] rounded-md border border-hair bg-surface p-2.5" title={hint}>
      <div className="flex items-baseline justify-between gap-2">
        <h4 className="text-[13px] font-semibold leading-tight">{title}</h4>
        <span className="text-[20px] font-semibold tracking-tight tnum">{count}</span>
      </div>
      {extra}
      <div className="mt-1.5 flex min-h-[40px] flex-wrap content-start gap-0.5">{children}</div>
    </div>
  );
}

/** Incoming, then the stages of the real pipeline in the order a request meets them. The numbers and the dots are
 *  the requests the event log says are in that stage right now. */
export function Flow({ model, selected, onPick }: { model: LiveModel; version?: number; selected: string | null; onPick: (id: string) => void }) {
  const counts = model.counts();
  const rate = model.arrivalsPerMin(30);
  const recent = model.recentFinished(36);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-stretch gap-2">
        <div className="flex min-w-0 flex-1 basis-[9rem] flex-col justify-between rounded-md border border-hair bg-surface p-2.5" title="Requests that have arrived since the run started">
          <h4 className="text-[13px] font-semibold leading-tight">Incoming</h4>
          <div>
            <div className="text-[20px] font-semibold tracking-tight tnum">{counts.arrived.toLocaleString()}</div>
            <div className="text-[12px] text-ink2 tnum">{rate === null ? "none yet" : `${rate.toFixed(1)} per minute, last 30 s`}</div>
          </div>
        </div>
        {NODES.map((n) => {
          const open = model.inNode(n.id as NodeId);
          const byState = counts.nodes[n.id];
          const detail = (["processing", "queued", "retrying"] as LiveState[]).filter((s) => byState[s] > 0).map((s) => `${byState[s]} ${STATE_META[s].label.toLowerCase()}`).join(", ");
          return (
            <NodeBox key={n.id} title={n.label} hint={n.hint} count={open.length} extra={<div className="text-[12px] text-ink2 min-h-[16px]">{detail || "empty"}</div>}>
              {open.slice(0, MAX_DOTS).map((r) => <Dot key={r.id} r={r} selected={selected === r.id} onPick={onPick} />)}
              {open.length > MAX_DOTS && <span className="text-[12px] text-ink2 self-center">+{open.length - MAX_DOTS} more</span>}
            </NodeBox>
          );
        })}
      </div>

      <div className="rounded-md border border-hair bg-surface p-2.5">
        <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
          <h4 className="text-[13px] font-semibold">Finished</h4>
          {(["successful", "rejected", "failed"] as const).map((s) => (
            <span key={s} className="flex items-center gap-1.5 text-[13px]" title={STATE_META[s].hint}>
              <Glyph state={s} />
              {STATE_META[s].label} <span className="font-semibold tnum">{counts.finished[s].toLocaleString()}</span>
            </span>
          ))}
          <span className="text-[12px] text-ink2">{counts.open} still in the pipeline</span>
        </div>
        <div className="mt-1.5 flex flex-wrap gap-0.5">
          {recent.length === 0 && <span className="text-[12px] text-ink2">Nothing has finished yet.</span>}
          {recent.map((r) => <Dot key={r.id} r={r} selected={selected === r.id} onPick={onPick} />)}
        </div>
      </div>
    </div>
  );
}
