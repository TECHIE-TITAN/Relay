import { useMemo, useState } from "react";
import { Flow, Glyph, STATE_META, StateLegend } from "../components/Flow";
import { LatencyChart, LimitEvents, RetriesChart, RpmChart, ThroughputChart } from "../components/SimCharts";
import { Notice, Provenance, SourceTag } from "../components/ui";
import type { LiveReq } from "../live";
import { fmtMs } from "../stats";
import { useLiveRun } from "../useLiveRun";
import { LoadChart, WindowChart } from "./Overview";

/** One simulation, followed event by event. Everything on this page is computed from the runner's event log. */
export default function SimLive({ runId, label, running, onOpenTrace }: { runId: string; label?: string; running: boolean; onOpenTrace: (runId: string, requestId: string) => void }) {
  const { model, version, ended, interrupted, error } = useLiveRun(runId);
  const [picked, setPicked] = useState<string | null>(null);

  const view = useMemo(() => {
    if (!model) return null;
    return { series: model.series(), sum: model.summary(), samples: [...model.samples], limitLog: [...model.limitLog] };
  }, [model, version]); // eslint-disable-line react-hooks/exhaustive-deps

  if (error) return <Notice tone="error">Could not follow {runId}: the event stream closed. Is the API still running?</Notice>;
  if (!model || !view) return <p className="text-ink2">Opening the event stream.</p>;

  const elapsed = model.t0 === null ? 0 : model.lastT - model.t0;
  const live = !ended && running;
  const req = picked ? model.reqs.get(picked) ?? null : null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h3 className="text-[16px] font-semibold tracking-tight">{label ?? runId}</h3>
        <SourceTag source="stand-in" />
        <Provenance value="SIMULATED" />
        <span className="flex items-center gap-1.5 text-[13px] text-ink2">
          {live && <span className="live-dot inline-block h-2 w-2 rounded-full bg-c3" />}
          {live ? "Running" : ended ? (interrupted ? "Interrupted: the event log stopped growing" : "Finished") : "Replaying the log"}
          <span className="tnum">· {elapsed.toFixed(0)} s of run time · {model.eventCount.toLocaleString()} events</span>
        </span>
        <span className="tnum text-[12px] text-ink2">{runId}</span>
      </div>

      <StateLegend />
      <Flow model={model} version={version} selected={picked} onPick={setPicked} />

      {req && <Inspector req={req} runId={runId} onClose={() => setPicked(null)} onOpenTrace={onOpenTrace} />}

      <div className="grid gap-4 lg:grid-cols-2">
        <ThroughputChart series={view.series} />
        <LatencyChart series={view.series} />
        <LoadChart series={view.samples} />
        <WindowChart series={view.samples} />
        <RpmChart samples={view.samples} />
        <RetriesChart series={view.series} />
      </div>
      <LimitEvents series={view.series} log={view.limitLog} />
    </div>
  );
}

function Inspector({ req, runId, onClose, onOpenTrace }: { req: LiveReq; runId: string; onClose: () => void; onOpenTrace: (runId: string, requestId: string) => void }) {
  return (
    <section aria-label="Request inspector" className="rounded-md border border-hair bg-surface p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h4 className="flex items-center gap-2 text-[14px] font-semibold">
            <Glyph state={req.state} /> {req.qid || "request"} · {STATE_META[req.state].label}
            {req.status ? <span className="font-normal text-ink2">({req.status}{req.mode ? `, ${req.mode}` : ""})</span> : null}
          </h4>
          <p className="text-[12px] text-ink2 tnum">
            {req.id} · arrived {req.arrive.toFixed(1)} s into the run · {req.attempts} model call{req.attempts === 1 ? "" : "s"} · {req.retryEvents} retr{req.retryEvents === 1 ? "y" : "ies"}/regeneration{req.retryEvents === 1 ? "" : "s"}
            {req.total !== undefined && req.total !== null ? ` · total ${fmtMs(req.total)}` : ""}
            {req.shed ? ` · shed: ${req.shed}` : ""}
          </p>
        </div>
        <div className="flex gap-2">
          <button className="rounded-sm border border-hair px-2.5 py-0.5 text-[13px] hover:bg-grid" onClick={() => onOpenTrace(runId, req.id)}>Open full trace</button>
          <button className="rounded-sm border border-hair px-2.5 py-0.5 text-[13px] hover:bg-grid" onClick={onClose}>Close</button>
        </div>
      </div>
      <ol className="mt-2 max-h-52 overflow-y-auto text-[13px]">
        {req.events.map((e, i) => (
          <li key={i} className="grid grid-cols-[4.5rem_9rem_1fr] gap-2 border-b border-grid py-0.5">
            <span className="tnum text-ink2">{(e.t - req.arrive).toFixed(2)} s</span>
            <span className="font-medium">{e.event}</span>
            <span className="break-words text-ink2">{e.note}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}
