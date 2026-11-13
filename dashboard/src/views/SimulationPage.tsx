import { useCallback, useEffect, useState } from "react";
import { getJson, useApi } from "../api";
import { DataTable, Notice } from "../components/ui";
import { fmtMs, fmtNum } from "../stats";
import { PAIRS, SimOptions, SimSpec, SimStatus, startSim, stopSim } from "../sim";
import { useLiveRun } from "../useLiveRun";
import type { RunInfo } from "../types";
import SimConfig from "./SimConfig";
import SimLive from "./SimLive";

interface Pair {
  kindId: string;
  a: SimStatus;
  b: SimStatus;
}

/** Live simulation: configure, start, watch. Starting a simulation launches the existing runner against the stand-in
 *  (see src/relay/simulations.py); the page only reads the events that runner logs. It cannot reach the real service. */
export default function SimulationPage({ runs, onOpenTrace, onReloadRuns }: { runs: RunInfo[] | null; onOpenTrace: (runId: string, requestId: string) => void; onReloadRuns: () => void }) {
  const opts = useApi<SimOptions>("/api/simulations/options");
  const [sims, setSims] = useState<SimStatus[]>([]);
  const [view, setView] = useState<string | null>(null);
  const [pair, setPair] = useState<Pair | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    getJson<SimStatus[]>("/api/simulations").then(setSims).catch(() => undefined);
  }, []);
  useEffect(() => {
    if (!opts.data) return;
    refresh();
    const id = window.setInterval(refresh, 2000);
    return () => window.clearInterval(id);
  }, [opts.data, refresh]);

  const running = sims.filter((s) => s.status === "running").length;
  const byRun = new Map(sims.map((s) => [s.run_id, s]));
  const simRuns = (runs ?? []).filter((r) => r.origin === "simulation").slice(0, 12);

  const launch = async (specs: SimSpec[], kindId?: string) => {
    setBusy(true);
    setError(null);
    const started: SimStatus[] = [];
    try {
      for (const s of specs) started.push(await startSim(s));
      if (kindId && started.length === 2) setPair({ kindId, a: started[0], b: started[1] });
      else setPair(null);
      setView(started[0].run_id);
    } catch (e) {
      setError((e as Error).message + (started.length ? " (the first run of the pair did start; stop it or let it finish)" : ""));
      if (started.length) setView(started[0].run_id);
    } finally {
      setBusy(false);
      refresh();
      window.setTimeout(onReloadRuns, 800);
    }
  };

  if (opts.error)
    return (
      <Notice tone={opts.error.startsWith("404") ? "info" : "error"}>
        {opts.error.startsWith("404")
          ? "Simulations are switched off: the API was started with --read-only. Restart it as `python -m relay.cli dashboard-api` to start stand-in simulations from here."
          : `Could not read the simulation options: ${opts.error}`}
      </Notice>
    );
  if (!opts.data) return <p className="text-ink2">Loading.</p>;

  const viewRun = view ? byRun.get(view) ?? null : null;
  const kind = pair ? PAIRS.find((p) => p.id === pair.kindId) : undefined;

  return (
    <div className="space-y-5">
      <header>
        <h2 className="text-[22px] font-semibold tracking-tight">Live simulation</h2>
        <p className="max-w-[80ch] text-[13px] text-ink2">
          Start a run of the real pipeline against the stand-in model server and watch requests move through it. Nothing here is animated for show: each dot is a request, placed by the events the runner logs, and every chart is drawn from those events.
        </p>
      </header>

      <SimConfig
        options={opts.data}
        busy={busy}
        error={error}
        running={running}
        onStart={(spec) => launch([spec])}
        onPair={(kindId, spec) => {
          const k = PAIRS.find((p) => p.id === kindId);
          if (k) launch([k.a(spec), k.b(spec)], kindId);
        }}
      />

      <section aria-label="Simulation runs">
        <h3 className="mb-1 text-[15px] font-semibold tracking-tight">Your simulations</h3>
        {simRuns.length === 0 && sims.length === 0 ? (
          <p className="text-[13px] text-ink2">None yet. Start one above; finished runs stay here and open under Traces.</p>
        ) : (
          <ul className="flex flex-wrap gap-2">
            {(simRuns.length ? simRuns : []).map((r) => {
              const s = byRun.get(r.run_id);
              const live = s ? s.status === "running" : r.live;
              const mark = pair && (r.run_id === pair.a.run_id ? "A" : r.run_id === pair.b.run_id ? "B" : null);
              return (
                <li key={r.run_id} className={`flex items-center gap-2 rounded-sm border px-2 py-1 text-[13px] ${view === r.run_id ? "border-ink bg-grid" : "border-hair bg-surface"}`}>
                  <button onClick={() => setView(r.run_id)} className="text-left">
                    <span className="font-medium">{mark ? `${mark}: ` : ""}{r.scenario}</span>{" "}
                    <span className="text-ink2 tnum">{r.wall_start_utc?.slice(11, 19)} UTC · {live ? "running" : r.interrupted ? "interrupted" : s?.status ?? "finished"}</span>
                  </button>
                  {s && s.status === "running" && (
                    <button className="rounded-sm border border-hair px-1.5 text-[12px] hover:bg-grid" onClick={() => stopSim(s.sim_id).then(refresh).catch((e) => setError((e as Error).message))}>
                      Stop
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
        {sims.filter((s) => s.status === "failed").map((s) => (
          <div key={s.sim_id} className="mt-2"><Notice tone="error">Simulation {s.run_id} failed (exit code {s.returncode}). {s.log_tail}</Notice></div>
        ))}
      </section>

      {pair && kind && <Compare pair={pair} kind={kind.names} explain={kind.explain} byRun={byRun} />}

      {view ? (
        <SimLive key={view} runId={view} label={pair && kind ? (view === pair.a.run_id ? `A: ${kind.names[0]}` : view === pair.b.run_id ? `B: ${kind.names[1]}` : undefined) : undefined} running={viewRun ? viewRun.status === "running" : false} onOpenTrace={onOpenTrace} />
      ) : (
        <Notice>Start a simulation, or pick one from the list, to see the pipeline.</Notice>
      )}
    </div>
  );
}

function Compare({ pair, kind, explain, byRun }: { pair: Pair; kind: [string, string]; explain: string; byRun: Map<string, SimStatus> }) {
  const A = useLiveRun(pair.a.run_id);
  const B = useLiveRun(pair.b.run_id);
  const sa = A.model?.summary();
  const sb = B.model?.summary();
  const done = A.ended && B.ended;
  const rows: { metric: string; a: string; b: string }[] = [
    { metric: "Requests finished", a: String(sa?.n ?? "not measured"), b: String(sb?.n ?? "not measured") },
    { metric: "Succeeded", a: sa ? `${sa.ok} (${((sa.okRate ?? 0) * 100).toFixed(1)}%)` : "not measured", b: sb ? `${sb.ok} (${((sb.okRate ?? 0) * 100).toFixed(1)}%)` : "not measured" },
    { metric: "Rejected (shed or limited)", a: String(sa?.rejected ?? "not measured"), b: String(sb?.rejected ?? "not measured") },
    { metric: "Failed (timed out or errored)", a: String(sa?.failed ?? "not measured"), b: String(sb?.failed ?? "not measured") },
    { metric: "Total time, median", a: fmtMs(sa?.p50), b: fmtMs(sb?.p50) },
    { metric: "Total time, 95th percentile", a: fmtMs(sa?.p95), b: fmtMs(sb?.p95) },
    { metric: "Retries per request", a: fmtNum(sa?.retriesPerRequest, 2), b: fmtNum(sb?.retriesPerRequest, 2) },
    { metric: "Provider 429 responses", a: String(sa?.provider429 ?? "not measured"), b: String(sb?.provider429 ?? "not measured") },
    { metric: "Requests that regenerated", a: String(sa?.regenerated ?? "not measured"), b: String(sb?.regenerated ?? "not measured") },
    { metric: "Tokens consumed (simulated)", a: sa ? sa.tokens.toLocaleString() : "not measured", b: sb ? sb.tokens.toLocaleString() : "not measured" },
    { metric: "Peak queue length", a: String(sa?.peakQueue ?? "not measured"), b: String(sb?.peakQueue ?? "not measured") },
    { metric: "Peak requests in flight", a: String(sa?.peakInflight ?? "not measured"), b: String(sb?.peakInflight ?? "not measured") },
  ];
  const state = (id: string, ended: boolean) => (ended ? "finished" : byRun.get(id)?.status === "running" ? "running" : "replaying");
  return (
    <section aria-label="Comparison" className="rounded-md border border-hair bg-surface p-3">
      <h3 className="text-[15px] font-semibold tracking-tight">Comparison</h3>
      <p className="max-w-[80ch] text-[12px] text-ink2">{explain} {done ? "Both runs have finished." : "At least one run is still going, so these numbers are partial."} Both ran on the stand-in (SIMULATED) on the same machine at the same time.</p>
      <div className="mt-2">
        <DataTable
          rows={rows}
          rowKey={(r) => r.metric}
          cols={[
            { key: "m", head: "Measure", cell: (r) => r.metric },
            { key: "a", head: `A: ${kind[0]} (${state(pair.a.run_id, A.ended)})`, align: "right", cell: (r) => r.a },
            { key: "b", head: `B: ${kind[1]} (${state(pair.b.run_id, B.ended)})`, align: "right", cell: (r) => r.b },
          ]}
        />
      </div>
    </section>
  );
}
