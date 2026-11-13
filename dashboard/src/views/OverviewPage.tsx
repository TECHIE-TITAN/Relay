import { useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, ComposedChart, Legend as RLegend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useApi } from "../api";
import { byScenario, histogram, kpis, parseRows, throughputSeries } from "../aggregate";
import { ChartCard, DataTable, Notice, Provenance } from "../components/ui";
import { fmtMs, fmtNum } from "../stats";
import type { GatewaySample, RowsResponse } from "../types";
import { LoadChart, WindowChart } from "./Overview";

type Source = "all" | "real" | "stand-in";
type Origin = "evidence" | "simulation";

const STATUSES = [
  { key: "ok", label: "Succeeded", glyph: "●", color: "var(--st-ok)", text: "#ffffff" },
  { key: "rate_limited", label: "Rate-limited", glyph: "▲", color: "var(--st-rate)", text: "#0b0b0b" },
  { key: "timeout", label: "Timed out or given up", glyph: "■", color: "var(--st-timeout)", text: "#0b0b0b" },
  { key: "error", label: "Error", glyph: "✕", color: "var(--st-error)", text: "#ffffff" },
] as const;

const axis = { stroke: "var(--axis)", tick: { fill: "var(--ink2)", fontSize: 12 }, tickLine: false } as const;
const tip = { contentStyle: { background: "var(--surface)", border: "1px solid var(--hair)", fontSize: 12 }, cursor: { fill: "var(--grid)" } } as const;

function segLabel(color: string) {
  return (p: any) => {
    const { x, y, width, height, value } = p;
    if (!value || width < 26) return <g />; // Recharts' label type needs an element, not null: draw nothing
    return (
      <text x={x + width / 2} y={y + height / 2} textAnchor="middle" dominantBaseline="central" fontSize={11} fontWeight={600} fill={color}>
        {value}
      </text>
    );
  };
}

function Segmented<T extends string>({ label, value, options, onChange }: { label: string; value: T; options: { id: T; text: string }[]; onChange: (v: T) => void }) {
  return (
    <div className="flex items-center gap-2 text-[13px]">
      <span className="text-ink2">{label}</span>
      <div role="group" aria-label={label} className="flex border border-hair rounded-sm overflow-hidden">
        {options.map((o) => (
          <button key={o.id} aria-pressed={value === o.id} onClick={() => onChange(o.id)} className={`px-2.5 py-0.5 ${value === o.id ? "bg-ink text-surface" : "text-ink2 hover:bg-grid"}`}>
            {o.text}
          </button>
        ))}
      </div>
    </div>
  );
}

function Tile({ label, value, sub, strong }: { label: string; value: string; sub?: string; strong?: boolean }) {
  return (
    <div className="p-3.5 min-w-0">
      <div className="text-[13px] text-ink2">{label}</div>
      <div className={`mt-0.5 font-semibold tracking-tight leading-8 ${strong ? "text-[30px]" : "text-[24px]"}`}>{value}</div>
      {sub && <div className="text-[12px] text-ink2 mt-0.5">{sub}</div>}
    </div>
  );
}

const pct = (n: number, d: number) => (d ? `${((n / d) * 100).toFixed(1)}%` : "not measured");

/** Historical monitoring across the evidence runs (the latest run of each scenario). Nothing here is live and
 *  nothing is started from here: simulations started from the dashboard are a separate origin, off by default. */
export default function OverviewPage({ onOpenRun }: { onOpenRun: (runId: string) => void }) {
  const [source, setSource] = useState<Source>("all");
  const [origin, setOrigin] = useState<Origin>("evidence");
  const [scenario, setScenario] = useState("all");
  const { data, error, loading } = useApi<RowsResponse>(`/api/rows?source=${source}&origin=${origin}&scope=latest`);

  const all = useMemo(() => parseRows(data?.rows ?? []), [data]);
  const scenarios = useMemo(() => Array.from(new Set(all.map((r) => r.scenario))), [all]);
  const picked = scenario !== "all" && scenarios.includes(scenario) ? scenario : "all";
  const rows = useMemo(() => (picked === "all" ? all : all.filter((r) => r.scenario === picked)), [all, picked]);
  const k = useMemo(() => kpis(rows), [rows]);
  const groups = useMemo(() => byScenario(rows).sort((a, b) => b.n - a.n), [rows]);
  const hist = useMemo(() => histogram(rows.filter((r) => r.status === "ok" && r.ft !== null).map((r) => r.ft as number)), [rows]);
  const series = useMemo(() => (picked === "all" ? [] : throughputSeries(rows)), [rows, picked]);
  const runId = picked === "all" ? null : rows[0]?.run_id ?? null;
  const state = useApi<GatewaySample[]>(runId && data?.run_meta[runId] ? `/api/runs/${runId}/state` : null);

  const nReal = rows.filter((r) => r.source === "real").length;
  const nSim = rows.length - nReal;

  return (
    <div className="space-y-5">
      <header>
        <h2 className="text-[22px] font-semibold tracking-tight">Overview</h2>
        <p className="text-[13px] text-ink2 max-w-[75ch]">
          The latest run of each scenario, from the files in results/. Real rows come from the Groq probe and the three 20-question runs; stand-in rows are simulated.
        </p>
      </header>

      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 border-y border-hair py-2" role="group" aria-label="Filters">
        <Segmented<Source> label="Source" value={source} onChange={setSource} options={[{ id: "all", text: "All" }, { id: "real", text: "Real Groq runs" }, { id: "stand-in", text: "Stand-in" }]} />
        <Segmented<Origin> label="Runs" value={origin} onChange={setOrigin} options={[{ id: "evidence", text: "Evidence" }, { id: "simulation", text: "My simulations" }]} />
        <label className="flex items-center gap-2 text-[13px]">
          <span className="text-ink2">Scenario</span>
          <select className="bg-surface border border-hair rounded-sm px-1.5 py-0.5" value={picked} onChange={(e) => setScenario(e.target.value)}>
            <option value="all">All scenarios</option>
            {scenarios.map((s) => <option key={s}>{s}</option>)}
          </select>
        </label>
        {runId && <button className="text-[13px] underline underline-offset-2" onClick={() => onOpenRun(runId)}>Open this run in Traces</button>}
      </div>

      {error && <Notice tone="error">Could not read the runs: {error}. Is the API running (python -m relay.cli dashboard-api)?</Notice>}
      {!data && !error && <p className="text-ink2">Loading.</p>}
      {data && rows.length === 0 && (
        <Notice>
          {origin === "simulation" ? "No simulations yet. Start one on the Live Simulation page." : "No runs match this filter."}
        </Notice>
      )}

      {rows.length > 0 && (
        <div className={loading ? "held space-y-5" : "space-y-5"}>
          {nSim > 0 && (
            <p className="text-[13px] text-ink2 flex flex-wrap items-center gap-2">
              <Provenance value="SIMULATED" /> {nSim.toLocaleString()} of these {rows.length.toLocaleString()} requests ran on the stand-in, so their latency and token counts are simulated. {nReal > 0 && `${nReal} are real.`}
            </p>
          )}

          <section aria-label="Key figures" className="grid grid-cols-2 md:grid-cols-3 border border-hair rounded-md bg-surface divide-x divide-y divide-hair overflow-hidden">
            <Tile label="Total requests" value={k.total.toLocaleString()} sub={`${groups.length} scenario${groups.length === 1 ? "" : "s"}`} />
            <Tile strong label="Successful" value={k.ok.toLocaleString()} sub={`${pct(k.ok, k.total)} of requests`} />
            <Tile label="Errors" value={k.errors.toLocaleString()} sub={pct(k.errors, k.total)} />
            <Tile label="Timeouts" value={k.timeouts.toLocaleString()} sub={pct(k.timeouts, k.total)} />
            <Tile label="Rate limits" value={k.rateLimited.toLocaleString()} sub={pct(k.rateLimited, k.total)} />
            <Tile label="Average latency" value={fmtMs(k.avgLatency)} sub="total time, successful requests" />
            <Tile label="95th percentile latency" value={fmtMs(k.p95Latency)} sub={`over ${k.ok.toLocaleString()} successful${k.ok < 100 ? " (fewer than 100: no p99)" : ""}`} />
            <Tile label="Time to first token" value={fmtMs(k.ttftP50)} sub={`median; 95th percentile ${fmtMs(k.ttftP95)}`} />
            <Tile label="Tokens consumed" value={k.tokens.toLocaleString()} sub="provider-reported (simulated for the stand-in)" />
          </section>

          <div className="grid gap-4 lg:grid-cols-2">
            <ChartCard
              title={picked === "all" ? "Throughput by scenario" : "Throughput over time"}
              note={picked === "all" ? "Successful requests per minute while the run lasted. Real 20-question runs are sent back to back, so their rate is set by the token limit, not by an offered load." : "Requests started and requests that finished successfully, per 10 seconds."}
              chart={
                picked === "all" ? (
                  <div style={{ height: Math.max(180, groups.length * 26 + 40) }} role="img" aria-label="Successful requests per minute for each scenario">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={groups.map((g) => ({ name: g.scenario + (g.closedLoop ? " (back to back)" : ""), perMin: g.okPerMin === null ? 0 : Number(g.okPerMin.toFixed(1)) }))} layout="vertical" margin={{ top: 2, right: 16, bottom: 0, left: 8 }} barCategoryGap={5}>
                        <CartesianGrid stroke="var(--grid)" horizontal={false} />
                        <XAxis type="number" {...axis} />
                        <YAxis type="category" dataKey="name" width={150} {...axis} />
                        <Tooltip {...tip} formatter={(v: any) => `${v} per minute`} />
                        <Bar dataKey="perMin" name="Successful per minute" fill="var(--c1)" radius={[0, 3, 3, 0]} barSize={10} isAnimationActive={false} />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                ) : (
                  <div className="h-52" role="img" aria-label="Requests started and completed per 10 seconds">
                    <ResponsiveContainer width="100%" height="100%">
                      <ComposedChart data={series} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
                        <CartesianGrid stroke="var(--grid)" vertical={false} />
                        <XAxis dataKey="t" {...axis} tickFormatter={(v) => `${v}s`} />
                        <YAxis {...axis} width={28} allowDecimals={false} />
                        <Tooltip {...tip} labelFormatter={(v) => `${v} s into the run`} />
                        <RLegend iconType="plainline" wrapperStyle={{ fontSize: 12 }} />
                        <Line type="monotone" dataKey="started" name="Started" stroke="var(--c1)" strokeWidth={2} dot={false} isAnimationActive={false} />
                        <Line type="monotone" dataKey="completed" name="Completed successfully" stroke="var(--c2)" strokeWidth={2} dot={false} isAnimationActive={false} />
                      </ComposedChart>
                    </ResponsiveContainer>
                  </div>
                )
              }
              table={
                picked === "all" ? (
                  <DataTable rows={groups} rowKey={(g) => g.scenario} cols={[
                    { key: "s", head: "Scenario", cell: (g) => g.scenario },
                    { key: "n", head: "Sent", align: "right", cell: (g) => g.n },
                    { key: "o", head: "Succeeded", align: "right", cell: (g) => g.ok },
                    { key: "d", head: "Duration", align: "right", cell: (g) => (g.durationS === null ? "not measured" : `${g.durationS.toFixed(1)} s`) },
                    { key: "p", head: "Per minute", align: "right", cell: (g) => fmtNum(g.okPerMin, 1) },
                  ]} />
                ) : (
                  <DataTable rows={series} rowKey={(s) => String(s.t)} cols={[
                    { key: "t", head: "Seconds into the run", cell: (s) => s.t },
                    { key: "a", head: "Started", align: "right", cell: (s) => s.started },
                    { key: "b", head: "Completed", align: "right", cell: (s) => s.completed },
                  ]} />
                )
              }
            />

            <ChartCard
              title="Latency distribution"
              note="Time to first token of successful requests. Answers are released whole, so this is also when the person sees the reply. Real back-to-back runs include waiting for the token window."
              chart={
                <div className="h-52" role="img" aria-label="Number of successful requests by time to first token">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={hist} margin={{ top: 6, right: 8, bottom: 22, left: 0 }}>
                      <CartesianGrid stroke="var(--grid)" vertical={false} />
                      <XAxis dataKey="label" {...axis} interval={0} angle={-25} textAnchor="end" height={46} />
                      <YAxis {...axis} width={34} allowDecimals={false} />
                      <Tooltip {...tip} formatter={(v: any) => `${v} requests`} />
                      <Bar dataKey="count" name="Requests" fill="var(--c1)" radius={[3, 3, 0, 0]} barSize={26} isAnimationActive={false} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              }
              table={<DataTable rows={hist} rowKey={(h) => h.label} cols={[{ key: "l", head: "First token", cell: (h) => h.label }, { key: "c", head: "Requests", align: "right", cell: (h) => h.count }]} />}
            />

            <ChartCard
              title="Request outcomes"
              note="Share of each scenario's requests by how they ended. Counts are inside the bars where they fit."
              chart={
                <div style={{ height: Math.max(180, groups.length * 26 + 70) }} role="img" aria-label="Share of requests succeeded, rate-limited, timed out or failed, per scenario">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={groups.map((g) => ({ name: g.scenario, ok: g.ok, rate_limited: g.rateLimited, timeout: g.timeout, error: g.error }))} layout="vertical" stackOffset="expand" margin={{ top: 2, right: 16, bottom: 0, left: 8 }} barCategoryGap={5}>
                      <XAxis type="number" {...axis} tickFormatter={(v) => `${Math.round(v * 100)}%`} />
                      <YAxis type="category" dataKey="name" width={150} {...axis} />
                      <Tooltip {...tip} />
                      {STATUSES.map((s) => (
                        <Bar key={s.key} dataKey={s.key} name={s.label} stackId="o" fill={s.color} stroke="var(--surface)" strokeWidth={2} barSize={14} isAnimationActive={false} label={segLabel(s.text)} />
                      ))}
                    </BarChart>
                  </ResponsiveContainer>
                  <ul className="mt-1 flex flex-wrap gap-x-4 text-[13px] text-ink2" aria-label="Legend">
                    {STATUSES.map((s) => (
                      <li key={s.key} className="flex items-center gap-1.5"><span aria-hidden style={{ color: s.color }}>{s.glyph}</span>{s.label}</li>
                    ))}
                  </ul>
                </div>
              }
              table={<DataTable rows={groups} rowKey={(g) => g.scenario} cols={[
                { key: "s", head: "Scenario", cell: (g) => g.scenario },
                ...STATUSES.map((s) => ({ key: s.key, head: `${s.glyph} ${s.label}`, align: "right" as const, cell: (g: (typeof groups)[number]) => pct(s.key === "ok" ? g.ok : s.key === "rate_limited" ? g.rateLimited : s.key === "timeout" ? g.timeout : g.error, g.n) })),
              ]} />}
            />

            <ChartCard
              title="Token consumption"
              note="Provider-reported tokens of the requests in each scenario: what was sent (prompt and retrieved passages) and what came back."
              chart={
                <div style={{ height: Math.max(180, groups.length * 26 + 70) }} role="img" aria-label="Prompt and output tokens per scenario">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={groups.map((g) => ({ name: g.scenario, tokensIn: g.tokensIn, tokensOut: g.tokensOut }))} layout="vertical" margin={{ top: 2, right: 16, bottom: 0, left: 8 }} barCategoryGap={5}>
                      <CartesianGrid stroke="var(--grid)" horizontal={false} />
                      <XAxis type="number" {...axis} tickFormatter={(v) => (v >= 1000 ? `${Math.round(v / 1000)}K` : String(v))} />
                      <YAxis type="category" dataKey="name" width={150} {...axis} />
                      <Tooltip {...tip} formatter={(v: any) => `${Number(v).toLocaleString()} tokens`} />
                      <Bar dataKey="tokensIn" name="Prompt tokens" stackId="t" fill="var(--c1)" stroke="var(--surface)" strokeWidth={2} barSize={12} isAnimationActive={false} />
                      <Bar dataKey="tokensOut" name="Output tokens" stackId="t" fill="var(--c2)" stroke="var(--surface)" strokeWidth={2} barSize={12} isAnimationActive={false} />
                    </BarChart>
                  </ResponsiveContainer>
                  <ul className="mt-1 flex gap-4 text-[13px] text-ink2" aria-label="Legend">
                    <li className="flex items-center gap-1.5"><span className="inline-block h-2 w-5" style={{ background: "var(--c1)" }} />Prompt tokens</li>
                    <li className="flex items-center gap-1.5"><span className="inline-block h-2 w-5" style={{ background: "var(--c2)" }} />Output tokens</li>
                  </ul>
                </div>
              }
              table={<DataTable rows={groups} rowKey={(g) => g.scenario} cols={[
                { key: "s", head: "Scenario", cell: (g) => g.scenario },
                { key: "i", head: "Prompt", align: "right", cell: (g) => g.tokensIn.toLocaleString() },
                { key: "o", head: "Output", align: "right", cell: (g) => g.tokensOut.toLocaleString() },
                { key: "a", head: "Per successful request", align: "right", cell: (g) => fmtNum(g.ok ? (g.tokensIn + g.tokensOut) / g.ok : null, 0) },
              ]} />}
            />
          </div>

          <section aria-label="System load" className="space-y-3">
            <div>
              <h3 className="text-[15px] font-semibold tracking-tight">System load</h3>
              <p className="text-[13px] text-ink2 max-w-[75ch]">
                {picked === "all" ? "The most requests in the pipeline and the longest queue each scenario reached, from its run manifest. Pick a scenario above to see its load over time." : "Gateway samples logged once a second during the run."}
              </p>
            </div>
            {picked === "all" ? (
              <PeakLoad data={data} groups={groups.map((g) => g.scenario)} />
            ) : state.data && state.data.length > 0 ? (
              <div className="grid gap-4 lg:grid-cols-2"><WindowChart series={state.data} /><LoadChart series={state.data} /></div>
            ) : (
              <Notice>{runId && data?.run_meta[runId] ? (state.loading ? "Loading." : "This run has no gateway samples.") : "This scenario is the real probe: it has no gateway samples, so there is no load to show."}</Notice>
            )}
          </section>
        </div>
      )}
    </div>
  );
}

function PeakLoad({ data, groups }: { data: RowsResponse | null; groups: string[] }) {
  const meta = Object.values(data?.run_meta ?? {}).filter((m) => groups.includes(m.scenario));
  const rows = meta.filter((m) => m.peak_inflight !== null && m.peak_inflight !== undefined).map((m) => ({ name: m.scenario, inflight: m.peak_inflight as number, queue: m.peak_queue_len ?? 0 }));
  if (rows.length === 0) return <Notice>No run in this selection recorded its peak load.</Notice>;
  return (
    <ChartCard
      title="Peak load by scenario"
      chart={
        <div style={{ height: Math.max(160, rows.length * 28 + 60) }} role="img" aria-label="Peak requests in flight and peak queue length per scenario">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={rows} layout="vertical" margin={{ top: 2, right: 16, bottom: 0, left: 8 }} barCategoryGap={6} barGap={2}>
              <CartesianGrid stroke="var(--grid)" horizontal={false} />
              <XAxis type="number" {...axis} allowDecimals={false} />
              <YAxis type="category" dataKey="name" width={150} {...axis} />
              <Tooltip {...tip} />
              <Bar dataKey="inflight" name="Peak in flight" fill="var(--c1)" barSize={9} radius={[0, 3, 3, 0]} isAnimationActive={false} />
              <Bar dataKey="queue" name="Peak queue length" fill="var(--c2)" barSize={9} radius={[0, 3, 3, 0]} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
          <ul className="mt-1 flex gap-4 text-[13px] text-ink2" aria-label="Legend">
            <li className="flex items-center gap-1.5"><span className="inline-block h-2 w-5" style={{ background: "var(--c1)" }} />Peak in flight</li>
            <li className="flex items-center gap-1.5"><span className="inline-block h-2 w-5" style={{ background: "var(--c2)" }} />Peak queue length</li>
          </ul>
        </div>
      }
      table={<DataTable rows={rows} rowKey={(r) => r.name} cols={[{ key: "n", head: "Scenario", cell: (r) => r.name }, { key: "i", head: "Peak in flight", align: "right", cell: (r) => r.inflight }, { key: "q", head: "Peak queue", align: "right", cell: (r) => r.queue }]} />}
    />
  );
}
