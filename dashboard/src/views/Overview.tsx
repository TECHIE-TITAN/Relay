import { useEffect, useMemo, useRef, useState } from "react";
import { Area, CartesianGrid, ComposedChart, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useApi } from "../api";
import { Lanes, Legend } from "../components/Lanes";
import { ChartCard, DataTable, Figure, Notice, Provenance, SourceTag } from "../components/ui";
import { fmtMs, fmtNum, summarizeRows } from "../stats";
import type { GatewaySample, RequestRow, RunDetail, RunInfo } from "../types";

/** Overview of one run. A finished run is read once from the files. A live run is followed over SSE:
 *  gateway samples arrive as they are logged, and the request table is refetched whenever requests finish. */
export default function RunView({ run, onPick, onRunEnded }: { run: RunInfo; onPick: (r: RequestRow) => void; onRunEnded: () => void }) {
  const detail = useApi<RunDetail>(`/api/runs/${run.run_id}`);
  const requests = useApi<RequestRow[]>(`/api/runs/${run.run_id}/requests`);
  const restState = useApi<GatewaySample[]>(run.live ? null : `/api/runs/${run.run_id}/state`);
  const [live, setLive] = useState<GatewaySample[]>([]);
  const [lanesSort, setLanesSort] = useState<"recent" | "slowest">("recent");
  const buffer = useRef<GatewaySample[]>([]);
  const [finished, setFinished] = useState(0);
  const reloadRequests = useRef(requests.reload);
  reloadRequests.current = requests.reload;

  useEffect(() => {
    setLive([]);
    buffer.current = [];
    setFinished(0);
    if (!run.live) return;
    const es = new EventSource(`/api/runs/${run.run_id}/stream`);
    let refetch: number | undefined;
    es.onmessage = (m) => {
      try {
        const e = JSON.parse(m.data);
        if (e.event === "gateway_state") buffer.current.push(e as GatewaySample);
        else if (e.event === "done") {
          setFinished((f) => f + 1);
          window.clearTimeout(refetch);
          refetch = window.setTimeout(() => reloadRequests.current(), 700);
        }
      } catch {
        /* a partial line: ignore */
      }
    };
    es.addEventListener("end", () => {
      es.close();
      reloadRequests.current();
      onRunEnded();
    });
    const flush = window.setInterval(() => {
      if (buffer.current.length) {
        const add = buffer.current.splice(0);
        setLive((prev) => [...prev, ...add].slice(-3000));
      }
    }, 500);
    return () => {
      es.close();
      window.clearInterval(flush);
      window.clearTimeout(refetch);
    };
  }, [run.run_id, run.live]);

  const rows = requests.data ?? [];
  const sum = useMemo(() => summarizeRows(rows), [rows]);
  const series = run.live ? live : restState.data ?? [];
  const d = detail.data;
  const stale = requests.loading && rows.length > 0;

  return (
    <div className="space-y-5">
      <header>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-[22px] font-semibold tracking-tight">{run.scenario}</h2>
          <SourceTag source={run.source} />
          <Provenance value={d?.provenance ?? run.provenance} />
          {run.live && (
            <span className="flex items-center gap-1.5 text-[13px] text-ink2">
              <span className="live-dot inline-block h-2 w-2 rounded-full bg-c3" /> Live: {finished} finished since you opened it
            </span>
          )}
        </div>
        <p className="mt-1 text-[13px] text-ink2 max-w-[75ch]">{d?.description ?? ""}</p>
        <p className="mt-0.5 text-[12px] text-ink2 tnum">
          {run.run_id} · {d?.wall_start_utc?.replace("T", " ").slice(0, 19)} UTC
        </p>
      </header>

      {run.incomplete && (
        <Notice tone="warn">
          This run stopped early{d?.stop?.reason ? ` (${String(d.stop.reason).replace(/_/g, " ")})` : ""}. {d?.stop?.not_run?.length ?? 0} questions were not run.
        </Notice>
      )}
      {requests.error && <Notice tone="error">Could not read the requests: {requests.error}</Notice>}

      <section aria-label="Requests" className={stale ? "held" : ""}>
        <div className="flex flex-wrap items-end justify-between gap-3 mb-2">
          <div>
            <h3 className="text-[15px] font-semibold tracking-tight">Where each request spent its time</h3>
            <p className="text-[13px] text-ink2 max-w-[70ch]">
              One lane per request, on a shared time scale. Answers are buffered, verified and released whole, so all of a request's time is before its first token. Select a lane to open its trace.
            </p>
          </div>
          <div role="group" aria-label="Which requests" className="flex border border-hair rounded-sm overflow-hidden text-[13px]">
            {(["recent", "slowest"] as const).map((s) => (
              <button key={s} aria-pressed={lanesSort === s} onClick={() => setLanesSort(s)} className={`px-2.5 py-0.5 ${lanesSort === s ? "bg-ink text-surface" : "text-ink2 hover:bg-grid"}`}>
                {s === "recent" ? "Most recent 40" : "Slowest 40"}
              </button>
            ))}
          </div>
        </div>
        <Legend />
        <div className="mt-2">
          <Lanes rows={rows} sort={lanesSort} onPick={onPick} />
        </div>
      </section>

      <section aria-label="Figures" className="flex flex-wrap gap-x-10 gap-y-4 border-y border-hair py-4">
        <Figure label={`requests, ${sum.ok} ok`} value={sum.n} />
        <Figure label="first token, median" value={fmtMs(sum.ft.p50)} hint={`over ${sum.ft.n} ok requests`} />
        <Figure label="first token, 95th percentile" value={fmtMs(sum.ft.p95)} hint={sum.ft.n < 100 ? `${sum.ft.n} requests: p99 not reported below 100` : undefined} />
        <Figure label="retries per request" value={fmtNum(sum.retriesPerRequest, 2)} />
        <Figure label="tokens per answer" value={fmtNum(sum.tokensPerAnswer, 0)} hint="provider-reported tokens of released answers; free refusals count as 0" />
        <Figure
          label="not ok"
          value={sum.n - sum.ok}
          hint={Object.entries(sum.byStatus).filter(([k]) => k !== "ok").map(([k, v]) => `${k}: ${v}`).join(", ") || "none"}
        />
      </section>
      <p className="text-[12px] text-ink2 -mt-3">
        Answers by mode: {Object.entries(sum.modes).map(([k, v]) => `${k} ${v}`).join(", ") || "none yet"}.
      </p>

      <div className="grid gap-4 lg:grid-cols-2">
        <WindowChart series={series} />
        <LoadChart series={series} />
      </div>

      {d?.invariants && d.invariants.length > 0 && (
        <section aria-label="Invariants">
          <h3 className="text-[15px] font-semibold tracking-tight mb-1">Invariants, recomputed from the event log</h3>
          <DataTable
            rows={d.invariants}
            rowKey={(i) => i.id}
            cols={[
              { key: "id", head: "Check", cell: (i) => i.id },
              { key: "t", head: "What it says", cell: (i) => i.description ?? "" },
              { key: "r", head: "Result", cell: (i) => (!i.applicable ? "not applicable" : i.passed ? "passed" : "FAILED") },
              { key: "c", head: "Checked", align: "right", cell: (i) => i.checked ?? "" },
            ]}
          />
        </section>
      )}
    </div>
  );
}

function axisProps() {
  return { stroke: "var(--axis)", tick: { fill: "var(--ink2)", fontSize: 12 }, tickLine: false } as const;
}

export function WindowChart({ series }: { series: GatewaySample[] }) {
  const data = useMemo(() => series.filter((s) => s.tpm_used !== undefined).map((s) => ({ t: Math.round(s.t_mono), used: Math.round(s.tpm_used ?? 0), limit: Math.round(s.tpm_limit ?? 0) })), [series]);
  return (
    <ChartCard
      title="Tokens in the 60 second window"
      note="Tokens the gateway has reserved or spent in the last minute, against the limit it is working to (it moves when the provider's headers change it)."
      chart={
        data.length === 0 ? (
          <p className="text-[13px] text-ink2">No gateway samples in this run.</p>
        ) : (
          <div className="h-52" role="img" aria-label="Tokens used in the sliding window over time, against the limit">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid stroke="var(--grid)" vertical={false} />
                <XAxis dataKey="t" {...axisProps()} tickFormatter={(v) => `${v}s`} />
                <YAxis {...axisProps()} width={44} />
                <Tooltip contentStyle={{ background: "var(--surface)", border: "1px solid var(--hair)", fontSize: 12 }} labelFormatter={(v) => `${v} s into the run`} />
                <Area type="stepAfter" dataKey="used" name="Tokens in window" stroke="var(--ink)" strokeWidth={1.5} fill="var(--ink)" fillOpacity={0.12} isAnimationActive={false} />
                <Line type="stepAfter" dataKey="limit" name="Limit in use" stroke="var(--muted)" strokeWidth={1} dot={false} isAnimationActive={false} />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        )
      }
      table={<SampleTable rows={data.map((d) => ({ t: d.t, a: d.used, b: d.limit }))} a="Tokens in window" b="Limit in use" />}
    />
  );
}

export function LoadChart({ series }: { series: GatewaySample[] }) {
  const data = useMemo(() => series.filter((s) => s.inflight !== undefined).map((s) => ({ t: Math.round(s.t_mono), inflight: s.inflight ?? 0, queue: s.queue_len ?? 0 })), [series]);
  return (
    <ChartCard
      title="Requests in the pipeline and waiting at the door"
      note="Requests being worked on (limit set by the admission cap) and requests queued before them."
      chart={
        data.length === 0 ? (
          <p className="text-[13px] text-ink2">No gateway samples in this run.</p>
        ) : (
          <div className="h-52" role="img" aria-label="Requests in flight and queue length over time">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid stroke="var(--grid)" vertical={false} />
                <XAxis dataKey="t" {...axisProps()} tickFormatter={(v) => `${v}s`} />
                <YAxis {...axisProps()} width={28} allowDecimals={false} />
                <Tooltip contentStyle={{ background: "var(--surface)", border: "1px solid var(--hair)", fontSize: 12 }} labelFormatter={(v) => `${v} s into the run`} />
                <Line type="stepAfter" dataKey="inflight" name="In flight" stroke="var(--c1)" strokeWidth={2} dot={false} isAnimationActive={false} />
                <Line type="stepAfter" dataKey="queue" name="Queued" stroke="var(--c2)" strokeWidth={2} dot={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
            <ul className="mt-1 flex gap-4 text-[13px] text-ink2" aria-label="Legend">
              <li className="flex items-center gap-1.5"><span className="inline-block h-0.5 w-5 bg-c1" />In flight</li>
              <li className="flex items-center gap-1.5"><span className="inline-block h-0.5 w-5 bg-c2" />Queued</li>
            </ul>
          </div>
        )
      }
      table={<SampleTable rows={data.map((d) => ({ t: d.t, a: d.inflight, b: d.queue }))} a="In flight" b="Queued" />}
    />
  );
}

function SampleTable({ rows, a, b }: { rows: { t: number; a: number; b: number }[]; a: string; b: string }) {
  const thin = rows.filter((_, i) => i % Math.max(1, Math.ceil(rows.length / 60)) === 0);
  return (
    <div className="max-h-52 overflow-y-auto">
      <DataTable
        rows={thin}
        rowKey={(r) => String(r.t)}
        cols={[
          { key: "t", head: "Seconds into the run", cell: (r) => r.t },
          { key: "a", head: a, align: "right", cell: (r) => r.a.toLocaleString() },
          { key: "b", head: b, align: "right", cell: (r) => r.b.toLocaleString() },
        ]}
      />
      {rows.length > thin.length && <p className="text-[12px] text-ink2 mt-1">Every {Math.ceil(rows.length / 60)}th sample shown ({rows.length} logged).</p>}
    </div>
  );
}
