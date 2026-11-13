import { useCallback, useEffect, useState } from "react";
import { getJson, useApi } from "./api";
import { Notice } from "./components/ui";
import Experiments from "./views/Experiments";
import Limits from "./views/Limits";
import OverviewPage from "./views/OverviewPage";
import SimulationPage from "./views/SimulationPage";
import TracesPage from "./views/TracesPage";
import type { RunInfo } from "./types";

type Page = "overview" | "live" | "experiments" | "traces" | "limits";
const PAGES: { id: Page; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "live", label: "Live simulation" },
  { id: "experiments", label: "Experiments" },
  { id: "traces", label: "Traces" },
  { id: "limits", label: "Limits" },
];

function useTheme(): [string, () => void] {
  const [theme, setTheme] = useState<string>(() => {
    try {
      return localStorage.getItem("relay-theme") ?? "system";
    } catch {
      return "system";
    }
  });
  useEffect(() => {
    const el = document.documentElement;
    if (theme === "system") el.removeAttribute("data-theme");
    else el.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("relay-theme", theme);
    } catch {
      /* private window: the choice just is not remembered */
    }
  }, [theme]);
  return [theme, () => setTheme((t) => (t === "system" ? "dark" : t === "dark" ? "light" : "system"))];
}

export default function App() {
  const [page, setPage] = useState<Page>("overview");
  const [runs, setRuns] = useState<RunInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [request, setRequest] = useState<string | null>(null);
  const [theme, cycleTheme] = useTheme();
  const health = useApi<{ ok: boolean; simulations_enabled: boolean }>("/api/health");

  const loadRuns = useCallback(() => {
    getJson<RunInfo[]>("/api/runs")
      .then((r) => {
        setRuns(r);
        setError(null);
        setRunId((cur) => cur ?? r.find((x) => x.origin !== "simulation")?.run_id ?? r[0]?.run_id ?? null);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    loadRuns();
  }, [loadRuns]);
  useEffect(() => {
    if (!runs?.some((r) => r.live)) return;
    const id = window.setInterval(loadRuns, 5000); // a live run changes the list when it ends
    return () => window.clearInterval(id);
  }, [runs, loadRuns]);

  const openRun = (id: string) => {
    setRunId(id);
    setRequest(null);
    setPage("traces");
  };
  const openTrace = (id: string, requestId: string) => {
    setRunId(id);
    setRequest(requestId);
    setPage("traces");
    loadRuns(); // a simulation started a moment ago may not be in the list yet
  };

  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-30 border-b border-hair bg-surface">
        <div className="mx-auto flex max-w-[1240px] items-center gap-6 px-4 md:px-6">
          <h1 className="py-3 text-[18px] font-semibold tracking-tight">Relay</h1>
          <nav aria-label="Pages" className="flex flex-1 gap-1 overflow-x-auto">
            {PAGES.map((p) => (
              <button
                key={p.id}
                onClick={() => setPage(p.id)}
                aria-current={page === p.id ? "page" : undefined}
                className={`whitespace-nowrap px-3 py-3 text-[14px] border-b-2 ${page === p.id ? "border-ink font-semibold" : "border-transparent text-ink2 hover:text-ink"}`}
              >
                {p.label}
                {p.id === "live" && runs?.some((r) => r.live && r.origin === "simulation") && <span className="live-dot ml-1.5 inline-block h-1.5 w-1.5 rounded-full bg-c3" title="A simulation is running" />}
              </button>
            ))}
          </nav>
          <span className="hidden text-[12px] text-ink2 sm:inline" title="History is read-only. Simulations always use the stand-in and never call the provider.">
            {health.error ? "API unreachable" : health.data?.simulations_enabled ? "Simulations on" : "Read-only"}
          </span>
          <button onClick={cycleTheme} className="text-[12px] text-ink2 border border-hair rounded-sm px-1.5 py-0.5 hover:bg-grid" aria-label={`Colour theme: ${theme}. Change`}>
            {theme === "system" ? "Theme: system" : `Theme: ${theme}`}
          </button>
        </div>
      </header>

      <main className="mx-auto max-w-[1240px] px-4 py-5 md:px-6 md:py-6">
        {error && (
          <div className="mb-4">
            <Notice tone="error">Cannot reach the API ({error}). Start it with <code>python -m relay.cli dashboard-api</code> and reload.</Notice>
          </div>
        )}
        {page === "overview" && <OverviewPage onOpenRun={openRun} />}
        {page === "live" && <SimulationPage runs={runs} onReloadRuns={loadRuns} onOpenTrace={openTrace} />}
        {page === "experiments" && <Experiments />}
        {page === "traces" && <TracesPage runs={runs} runId={runId} onRun={setRunId} request={request} onRequest={setRequest} onRunEnded={loadRuns} />}
        {page === "limits" && <Limits runs={runs} />}
        <footer className="mt-10 border-t border-hair pt-3 text-[12px] text-ink2">
          Every number comes from files in results/. History is read-only; simulations run on the stand-in and cannot reach the provider.
        </footer>
      </main>
    </div>
  );
}
