import { useMemo, useState } from "react";
import { Area, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ChartCard, DataTable, Figure, Notice, SourceTag } from "../components/ui";
import { DailyChart, LimitEvents, PauseChart, RetriesChart, RpmChart } from "../components/SimCharts";
import { useLiveRun } from "../useLiveRun";
import type { GatewaySample, RunInfo } from "../types";

const axis = { stroke: "var(--axis)", tick: { fill: "var(--ink2)", fontSize: 12 }, tickLine: false } as const;
const tip = { contentStyle: { background: "var(--surface)", border: "1px solid var(--hair)", fontSize: 12 }, cursor: { fill: "var(--grid)" } } as const;

function TpmChart({ samples }: { samples: GatewaySample[] }) {
  const all = samples.filter((s) => s.tpm_used !== undefined).map((s) => ({ t: Math.round(s.t_mono), used: Math.round(s.tpm_used ?? 0), limit: Math.round(s.tpm_limit ?? 0) }));
  const every = Math.max(1, Math.ceil(all.length / 400));
  const data = all.filter((_, i) => i % every === 0);
  return (
    <ChartCard
      title="Tokens in the 60 second window"
      note="Tokens the gateway has reserved or spent in the last minute (prompt plus the reply it may need), against the limit it is working to."
      chart={
        data.length === 0 ? (
          <p className="text-[13px] text-ink2">No gateway samples in this run.</p>
        ) : (
          <div className="h-48" role="img" aria-label="Tokens in the sliding window over time, against the limit">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid stroke="var(--grid)" vertical={false} />
                <XAxis dataKey="t" {...axis} tickFormatter={(v) => `${v}s`} />
                <YAxis {...axis} width={52} tickFormatter={(v) => (v >= 1000 ? `${Math.round(v / 1000)}K` : String(v))} />
                <Tooltip {...tip} labelFormatter={(v) => `${v} s into the run`} />
                <Area type="stepAfter" dataKey="used" name="Tokens in window" stroke="var(--ink)" strokeWidth={1.5} fill="var(--ink)" fillOpacity={0.12} isAnimationActive={false} />
                <Line type="stepAfter" dataKey="limit" name="Limit in use" stroke="var(--muted)" strokeWidth={1} dot={false} isAnimationActive={false} />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        )
      }
      table={<DataTable rows={data} rowKey={(d) => String(d.t)} cols={[{ key: "t", head: "Seconds", cell: (d) => d.t }, { key: "u", head: "In window", align: "right", cell: (d) => d.used.toLocaleString() }, { key: "l", head: "Limit in use", align: "right", cell: (d) => d.limit.toLocaleString() }]} />}
    />
  );
}

/** Gateway behaviour of one run, rebuilt from its logged events only. */
export default function GatewayRun({ runs }: { runs: RunInfo[] | null }) {
  const choices = useMemo(() => (runs ?? []).filter((r) => (r.rows ?? 0) > 0 || r.live), [runs]);
  const [picked, setPicked] = useState<string | null>(null);
  const runId = picked ?? choices.find((r) => r.origin === "simulation")?.run_id ?? choices[0]?.run_id ?? null;
  const info = choices.find((r) => r.run_id === runId) ?? null;
  const { model, version, ended, error } = useLiveRun(runId);
  void version; // the model is mutated in place; the version bump is what re-renders this view
  const sum = model?.summary();
  const standIn = info?.source !== "real";

  return (
    <section className="space-y-3" aria-label="Gateway behaviour in one run">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h3 className="text-[18px] font-semibold tracking-tight">Gateway behaviour in one run</h3>
          <p className="text-[13px] text-ink2 max-w-[75ch]">
            What the rate limiter, retries and circuit breaker did, rebuilt from the run's event log. A stand-in run shows simulated behaviour against simulated limits; a real run shows behaviour against the documented limits above.
          </p>
        </div>
        <label className="text-[13px] text-ink2">
          Run{" "}
          <select value={runId ?? ""} onChange={(e) => setPicked(e.target.value)} className="ml-1 rounded-sm border border-hair bg-surface px-1.5 py-1 text-ink">
            {choices.map((r) => (
              <option key={r.run_id} value={r.run_id}>{r.scenario} ({r.source}{r.origin === "simulation" ? ", from the dashboard" : ""}) {r.run_id}</option>
            ))}
          </select>
        </label>
      </header>

      {!runId && <Notice>No run with logged events yet. Start a simulation on the Live simulation page, or run a scenario from the CLI.</Notice>}
      {error && <Notice tone="error">The event stream for this run could not be read.</Notice>}

      {runId && model && sum && (
        <>
          <div className="flex flex-wrap items-center gap-3 text-[13px] text-ink2">
            {info && <SourceTag source={info.source} />}
            <span className={`inline-block rounded-sm px-1.5 text-[12px] leading-5 ${standIn ? "border border-dashed border-ink2 text-ink2" : "border border-ink text-ink"}`}>
              {standIn ? "SIMULATED limits and failures" : "Real service: documented limits, observed behaviour"}
            </span>
            {!ended && <span>Still reading the log.</span>}
          </div>

          <div className="flex flex-wrap gap-x-8 gap-y-3 bg-surface border border-hair rounded-md p-4">
            <Figure label="Peak calls in flight" value={sum.peakInflight ?? "n/a"} hint="Highest in-flight count the gateway logged" />
            <Figure label="Peak admission queue" value={sum.peakQueue ?? "n/a"} />
            <Figure label="Provider 429s" value={sum.provider429} hint="Rate-limit responses seen in call_end events" />
            <Figure label="Circuit breaker opened" value={sum.breakerOpens} hint="Times the breaker tripped, from breaker_open events" />
            <Figure label="Global pauses" value={sum.pauses} hint="pause_start events" />
            <Figure label="Retries per request" value={sum.retriesPerRequest === null ? "n/a" : sum.retriesPerRequest.toFixed(2)} />
          </div>

          {model.samples.length === 0 && <Notice>This run logged no gateway samples, so the window, pause and budget charts below are empty.</Notice>}

          <div className="grid gap-4 lg:grid-cols-2">
            <RpmChart samples={model.samples} />
            <TpmChart samples={model.samples} />
            <PauseChart samples={model.samples} />
            <DailyChart samples={model.samples} />
            <RetriesChart series={model.series()} />
            <LimitEvents series={model.series()} log={model.limitLog} />
          </div>
        </>
      )}
    </section>
  );
}
