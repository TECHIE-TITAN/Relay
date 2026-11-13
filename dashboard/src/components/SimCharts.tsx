import { Area, Bar, BarChart, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ChartCard, DataTable } from "./ui";
import type { LogLine } from "../live";
import { fmtMs } from "../stats";
import type { GatewaySample } from "../types";

const axis = { stroke: "var(--axis)", tick: { fill: "var(--ink2)", fontSize: 12 }, tickLine: false } as const;
const tip = { contentStyle: { background: "var(--surface)", border: "1px solid var(--hair)", fontSize: 12 }, cursor: { fill: "var(--grid)" } } as const;
const Key = ({ items }: { items: { color: string; label: string; line?: boolean }[] }) => (
  <ul className="mt-1 flex flex-wrap gap-x-4 text-[13px] text-ink2" aria-label="Legend">
    {items.map((i) => (
      <li key={i.label} className="flex items-center gap-1.5">
        <span className={`inline-block ${i.line ? "h-0.5" : "h-2"} w-5`} style={{ background: i.color }} />
        {i.label}
      </li>
    ))}
  </ul>
);

interface SeriesPoint {
  t: number;
  arrived: number;
  completed: number;
  retries: number;
  limit: number;
  p50: number | null;
  p95: number | null;
}

function thin<T>(rows: T[], max = 60): T[] {
  const every = Math.max(1, Math.ceil(rows.length / max));
  return rows.filter((_, i) => i % every === 0);
}

export function ThroughputChart({ series }: { series: SeriesPoint[] }) {
  return (
    <ChartCard
      title="Incoming and completed requests"
      note="Per 5 seconds of the run: requests that arrived, and requests that finished successfully."
      chart={
        <div className="h-48" role="img" aria-label="Requests arrived and completed per five seconds">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={series} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="var(--grid)" vertical={false} />
              <XAxis dataKey="t" {...axis} tickFormatter={(v) => `${v}s`} />
              <YAxis {...axis} width={28} allowDecimals={false} />
              <Tooltip {...tip} labelFormatter={(v) => `${v} s into the run`} />
              <Line type="monotone" dataKey="arrived" name="Arrived" stroke="var(--c1)" strokeWidth={2} dot={false} isAnimationActive={false} />
              <Line type="monotone" dataKey="completed" name="Completed successfully" stroke="var(--c2)" strokeWidth={2} dot={false} isAnimationActive={false} />
            </ComposedChart>
          </ResponsiveContainer>
          <Key items={[{ color: "var(--c1)", label: "Arrived", line: true }, { color: "var(--c2)", label: "Completed successfully", line: true }]} />
        </div>
      }
      table={<DataTable rows={thin(series)} rowKey={(s) => String(s.t)} cols={[{ key: "t", head: "Seconds", cell: (s) => s.t }, { key: "a", head: "Arrived", align: "right", cell: (s) => s.arrived }, { key: "c", head: "Completed", align: "right", cell: (s) => s.completed }]} />}
    />
  );
}

export function LatencyChart({ series }: { series: SeriesPoint[] }) {
  const has = series.some((s) => s.p50 !== null);
  return (
    <ChartCard
      title="Latency of successful requests"
      note="Total time per request, median and 95th percentile within each 5 seconds. A gap means nothing finished successfully in that window."
      chart={
        !has ? (
          <p className="text-[13px] text-ink2">No request has succeeded yet.</p>
        ) : (
          <div className="h-48" role="img" aria-label="Median and 95th percentile total time of successful requests over the run">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={series} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid stroke="var(--grid)" vertical={false} />
                <XAxis dataKey="t" {...axis} tickFormatter={(v) => `${v}s`} />
                <YAxis {...axis} width={46} tickFormatter={(v) => fmtMs(v)} />
                <Tooltip {...tip} formatter={(v: any) => fmtMs(Number(v))} labelFormatter={(v) => `${v} s into the run`} />
                <Line type="monotone" dataKey="p50" name="Median" stroke="var(--c1)" strokeWidth={2} dot={{ r: 2 }} connectNulls isAnimationActive={false} />
                <Line type="monotone" dataKey="p95" name="95th percentile" stroke="var(--c2)" strokeWidth={2} dot={{ r: 2 }} connectNulls isAnimationActive={false} />
              </ComposedChart>
            </ResponsiveContainer>
            <Key items={[{ color: "var(--c1)", label: "Median", line: true }, { color: "var(--c2)", label: "95th percentile", line: true }]} />
          </div>
        )
      }
      table={<DataTable rows={thin(series.filter((s) => s.p50 !== null))} rowKey={(s) => String(s.t)} cols={[{ key: "t", head: "Seconds", cell: (s) => s.t }, { key: "p", head: "Median", align: "right", cell: (s) => fmtMs(s.p50) }, { key: "q", head: "95th percentile", align: "right", cell: (s) => fmtMs(s.p95) }]} />}
    />
  );
}

export function RpmChart({ samples }: { samples: GatewaySample[] }) {
  const data = thin(samples.filter((s) => s.rpm_used !== undefined).map((s) => ({ t: Math.round(s.t_mono), used: Math.round((s.rpm_used ?? 0) * 10) / 10, limit: Math.round(s.rpm_limit ?? 0) })), 400);
  return (
    <ChartCard
      title="Requests in the 60 second window"
      note="Provider calls the gateway has counted in the last minute, against the limit it is working to."
      chart={
        data.length === 0 ? (
          <p className="text-[13px] text-ink2">No gateway samples yet.</p>
        ) : (
          <div className="h-48" role="img" aria-label="Requests in the sliding window over time, against the limit">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid stroke="var(--grid)" vertical={false} />
                <XAxis dataKey="t" {...axis} tickFormatter={(v) => `${v}s`} />
                <YAxis {...axis} width={40} />
                <Tooltip {...tip} labelFormatter={(v) => `${v} s into the run`} />
                <Area type="stepAfter" dataKey="used" name="Requests in window" stroke="var(--ink)" strokeWidth={1.5} fill="var(--ink)" fillOpacity={0.12} isAnimationActive={false} />
                <Line type="stepAfter" dataKey="limit" name="Limit in use" stroke="var(--muted)" strokeWidth={1} dot={false} isAnimationActive={false} />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        )
      }
      table={<DataTable rows={thin(data)} rowKey={(s) => String(s.t)} cols={[{ key: "t", head: "Seconds", cell: (s) => s.t }, { key: "u", head: "In window", align: "right", cell: (s) => s.used }, { key: "l", head: "Limit in use", align: "right", cell: (s) => s.limit }]} />}
    />
  );
}

export function DailyChart({ samples }: { samples: GatewaySample[] }) {
  const withCap = samples.filter((s) => s.daily_tokens !== undefined && (s.daily_token_cap ?? 0) > 0 && (s.daily_token_cap ?? 0) < 1e9);
  const data = thin(withCap.map((s) => ({ t: Math.round(s.t_mono), used: s.daily_tokens ?? 0, cap: s.daily_token_cap ?? 0 })), 400);
  return (
    <ChartCard
      title="Daily token budget"
      note="Tokens the gateway's client-side ledger counted for the day, against its cap. Only runs with a real daily cap have one."
      chart={
        data.length === 0 ? (
          <p className="text-[13px] text-ink2">This run has no daily token cap on the client side (a stand-in run leaves the stand-in's own limit as the signal), so there is nothing to plot.</p>
        ) : (
          <div className="h-48" role="img" aria-label="Daily tokens counted against the cap over the run">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid stroke="var(--grid)" vertical={false} />
                <XAxis dataKey="t" {...axis} tickFormatter={(v) => `${v}s`} />
                <YAxis {...axis} width={52} tickFormatter={(v) => (v >= 1000 ? `${Math.round(v / 1000)}K` : String(v))} />
                <Tooltip {...tip} labelFormatter={(v) => `${v} s into the run`} />
                <Area type="stepAfter" dataKey="used" name="Tokens counted today" stroke="var(--ink)" strokeWidth={1.5} fill="var(--ink)" fillOpacity={0.12} isAnimationActive={false} />
                <Line type="stepAfter" dataKey="cap" name="Cap" stroke="var(--muted)" strokeWidth={1} dot={false} isAnimationActive={false} />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        )
      }
      table={<DataTable rows={thin(data)} rowKey={(d) => String(d.t)} cols={[{ key: "t", head: "Seconds", cell: (d) => d.t }, { key: "u", head: "Counted", align: "right", cell: (d) => d.used.toLocaleString() }, { key: "c", head: "Cap", align: "right", cell: (d) => d.cap.toLocaleString() }]} />}
    />
  );
}

export function PauseChart({ samples }: { samples: GatewaySample[] }) {
  const data = thin(samples.filter((s) => s.aimd_factor !== undefined).map((s) => ({ t: Math.round(s.t_mono), factor: s.aimd_factor ?? 1, paused: s.paused ? 1 : 0 })), 400);
  return (
    <ChartCard
      title="Rate cut and global pause"
      note="The share of the limit the gateway allows itself (1 means no cut; it drops after a 429 and recovers), and the moments all dispatch was paused."
      chart={
        data.length === 0 ? (
          <p className="text-[13px] text-ink2">No gateway samples in this run.</p>
        ) : (
          <div className="h-48" role="img" aria-label="Rate-cut factor and paused periods over the run">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid stroke="var(--grid)" vertical={false} />
                <XAxis dataKey="t" {...axis} tickFormatter={(v) => `${v}s`} />
                <YAxis domain={[0, 1]} {...axis} width={32} />
                <Tooltip {...tip} labelFormatter={(v) => `${v} s into the run`} />
                <Area type="stepAfter" dataKey="paused" name="Paused (1 = yes)" stroke="var(--ink2)" strokeWidth={1} fill="var(--ink2)" fillOpacity={0.25} isAnimationActive={false} />
                <Line type="stepAfter" dataKey="factor" name="Rate factor" stroke="var(--c2)" strokeWidth={2} dot={false} isAnimationActive={false} />
              </ComposedChart>
            </ResponsiveContainer>
            <Key items={[{ color: "var(--c2)", label: "Rate factor", line: true }, { color: "var(--ink2)", label: "Paused" }]} />
          </div>
        )
      }
      table={<DataTable rows={thin(data)} rowKey={(d) => String(d.t)} cols={[{ key: "t", head: "Seconds", cell: (d) => d.t }, { key: "f", head: "Rate factor", align: "right", cell: (d) => d.factor }, { key: "p", head: "Paused", align: "right", cell: (d) => (d.paused ? "yes" : "no") }]} />}
    />
  );
}

export function RetriesChart({ series }: { series: SeriesPoint[] }) {
  return (
    <ChartCard
      title="Retries and regenerations"
      note="Back-off retries after a failed call, and second drafts after a failed check, per 5 seconds."
      chart={
        <div className="h-48" role="img" aria-label="Retries and regenerations per five seconds">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={series} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="var(--grid)" vertical={false} />
              <XAxis dataKey="t" {...axis} tickFormatter={(v) => `${v}s`} />
              <YAxis {...axis} width={28} allowDecimals={false} />
              <Tooltip {...tip} labelFormatter={(v) => `${v} s into the run`} />
              <Bar dataKey="retries" name="Retries and regenerations" fill="var(--c3)" radius={[2, 2, 0, 0]} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      }
      table={<DataTable rows={thin(series)} rowKey={(s) => String(s.t)} cols={[{ key: "t", head: "Seconds", cell: (s) => s.t }, { key: "r", head: "Retries and regenerations", align: "right", cell: (s) => s.retries }]} />}
    />
  );
}

export function LimitEvents({ series, log }: { series: SeriesPoint[]; log: LogLine[] }) {
  const recent = log.slice(-12).reverse();
  return (
    <ChartCard
      title="Rate-limit events"
      note="Provider 429 responses, gateway pauses and limit changes per 5 seconds, and the latest events by name."
      chart={
        <div>
          <div className="h-36" role="img" aria-label="Rate-limit events per five seconds">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={series} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid stroke="var(--grid)" vertical={false} />
                <XAxis dataKey="t" {...axis} tickFormatter={(v) => `${v}s`} />
                <YAxis {...axis} width={28} allowDecimals={false} />
                <Tooltip {...tip} labelFormatter={(v) => `${v} s into the run`} />
                <Bar dataKey="limit" name="Rate-limit events" fill="var(--ink2)" radius={[2, 2, 0, 0]} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          </div>
          {recent.length === 0 ? (
            <p className="mt-2 text-[13px] text-ink2">No rate-limit events in this run.</p>
          ) : (
            <ul className="mt-2 space-y-0.5 text-[13px]">
              {recent.map((l, i) => (
                <li key={i} className="flex gap-2"><span aria-hidden>▲</span><span className="tnum text-ink2">{l.t.toFixed(1)} s</span><span className="font-medium">{l.event}</span><span className="text-ink2 break-words">{l.note}</span></li>
              ))}
            </ul>
          )}
        </div>
      }
      table={<DataTable rows={log.slice(-60).reverse().map((l, i) => ({ ...l, i }))} rowKey={(l) => `${l.i}-${l.t}`} cols={[{ key: "t", head: "Seconds", align: "right", cell: (l) => l.t.toFixed(1) }, { key: "e", head: "Event", cell: (l) => l.event }, { key: "n", head: "Detail", cell: (l) => l.note }]} />}
    />
  );
}
