import { useEffect, useState } from "react";
import { SourceTag } from "../components/ui";
import type { RequestRow, RunInfo } from "../types";
import RunView from "./Overview";
import Traces from "./Traces";

/** One run at a time: pick it here, see where its requests spent their time, then open any request's event log. */
export default function TracesPage({ runs, runId, onRun, request, onRequest, onRunEnded }: {
  runs: RunInfo[] | null;
  runId: string | null;
  onRun: (id: string) => void;
  request: string | null;
  onRequest: (id: string | null) => void;
  onRunEnded: () => void;
}) {
  const [tab, setTab] = useState<"lanes" | "requests">(request ? "requests" : "lanes");
  useEffect(() => {
    if (request) setTab("requests"); // arriving with a request selected (from a lane or from the simulation page)
  }, [request]);
  const run = runs?.find((r) => r.run_id === runId) ?? null;
  const evidence = (runs ?? []).filter((r) => r.origin !== "simulation");
  const sims = (runs ?? []).filter((r) => r.origin === "simulation");

  const label = (r: RunInfo) => `${r.scenario} · ${r.source === "real" ? "real" : "stand-in"} · ${r.wall_start_utc?.replace("T", " ").slice(5, 16) ?? "?"} UTC${r.live ? " · live" : ""}${r.interrupted ? " · interrupted" : ""}`;

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-[22px] font-semibold tracking-tight">Traces</h2>
          <p className="text-[13px] text-ink2 max-w-[70ch]">Every request of one run, from admission to release. Pick a run, then a request.</p>
        </div>
        <label className="flex items-center gap-2 text-[13px]">
          <span className="text-ink2">Run</span>
          <select className="bg-surface border border-hair rounded-sm px-1.5 py-1 max-w-[26rem]" value={runId ?? ""} onChange={(e) => { onRun(e.target.value); onRequest(null); }}>
            {evidence.length > 0 && <optgroup label="Evidence runs">{evidence.map((r) => <option key={r.run_id} value={r.run_id}>{label(r)}</option>)}</optgroup>}
            {sims.length > 0 && <optgroup label="My simulations">{sims.map((r) => <option key={r.run_id} value={r.run_id}>{label(r)}</option>)}</optgroup>}
          </select>
        </label>
      </header>

      {!runs && <p className="text-ink2">Loading.</p>}
      {runs && runs.length === 0 && <p className="text-ink2">No runs yet. Run a scenario or start a simulation, then reload.</p>}

      {run && (
        <>
          <div className="flex items-center gap-3 border-b border-hair">
            {([["lanes", "Run view"], ["requests", "Requests and event log"]] as const).map(([id, text]) => (
              <button key={id} onClick={() => setTab(id)} aria-current={tab === id ? "page" : undefined}
                className={`px-1 py-1.5 text-[14px] -mb-px border-b-2 ${tab === id ? "border-ink font-semibold" : "border-transparent text-ink2 hover:text-ink"}`}>
                {text}
              </button>
            ))}
            <span className="ml-auto pb-1"><SourceTag source={run.source} /></span>
          </div>
          {tab === "lanes" ? (
            <RunView key={run.run_id} run={run} onRunEnded={onRunEnded} onPick={(r: RequestRow) => { onRequest(r.request_id); setTab("requests"); }} />
          ) : (
            <Traces key={run.run_id} run={run} request={request} onSelect={onRequest} />
          )}
        </>
      )}
    </div>
  );
}
