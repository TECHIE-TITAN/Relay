import { useMemo, useState } from "react";
import { useApi } from "../api";
import { Legend } from "../components/Lanes";
import { TraceDetail } from "../components/TraceDetail";
import { DataTable, Notice } from "../components/ui";
import { fmtMs } from "../stats";
import type { RequestRow, RunInfo, TraceResponse } from "../types";

/** Every request of a run in a table; select one for its whole lifecycle (stages, timings, passages, drafts, answer). */
export default function Traces({ run, request, onSelect }: { run: RunInfo; request: string | null; onSelect: (id: string | null) => void }) {
  const list = useApi<RequestRow[]>(`/api/runs/${run.run_id}/requests`);
  const trace = useApi<TraceResponse>(request ? `/api/runs/${run.run_id}/requests/${request}` : null);
  const [status, setStatus] = useState("all");
  const [sort, setSort] = useState<"total" | "arrival">("total");
  const [mode, setMode] = useState("all");

  const rows = list.data ?? [];
  const statuses = useMemo(() => ["all", ...Array.from(new Set(rows.map((r) => r.status)))], [rows]);
  const modes = useMemo(() => ["all", ...Array.from(new Set(rows.map((r) => r.answer_mode ?? "none")))], [rows]);
  const shown = useMemo(() => {
    const f = rows.filter((r) => (status === "all" || r.status === status) && (mode === "all" || (r.answer_mode ?? "none") === mode));
    return f.sort((a, b) => (sort === "total" ? b.total_ms - a.total_ms : a.arrive_t - b.arrive_t));
  }, [rows, status, mode, sort]);

  if (list.error) return <Notice tone="error">Could not read the requests: {list.error}</Notice>;

  return (
    <div className="space-y-5">
      <header>
        <h2 className="text-[22px] font-semibold tracking-tight">Traces: {run.scenario}</h2>
        <p className="text-[13px] text-ink2 max-w-[75ch]">Each row is one request from the event log. The split is the same one used in the write-up: waiting, working, retrying.</p>
      </header>

      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 border-y border-hair py-2 text-[13px]" role="group" aria-label="Filters">
        <label className="flex items-center gap-2">Status
          <select className="bg-surface border border-hair rounded-sm px-1.5 py-0.5" value={status} onChange={(e) => setStatus(e.target.value)}>
            {statuses.map((s) => <option key={s}>{s}</option>)}
          </select>
        </label>
        <label className="flex items-center gap-2">Answer
          <select className="bg-surface border border-hair rounded-sm px-1.5 py-0.5" value={mode} onChange={(e) => setMode(e.target.value)}>
            {modes.map((s) => <option key={s}>{s}</option>)}
          </select>
        </label>
        <label className="flex items-center gap-2">Order
          <select className="bg-surface border border-hair rounded-sm px-1.5 py-0.5" value={sort} onChange={(e) => setSort(e.target.value as "total" | "arrival")}>
            <option value="total">Slowest first</option>
            <option value="arrival">Arrival order</option>
          </select>
        </label>
        <span className="text-ink2">{shown.length} of {rows.length} requests</span>
      </div>

      <Legend />

      <DataTable
        rows={shown}
        rowKey={(r) => r.request_id}
        selected={request}
        onRow={(r) => onSelect(r.request_id === request ? null : r.request_id)}
        cols={[
          { key: "q", head: "Question", cell: (r) => r.question_id ?? "-" },
          { key: "m", head: "Answer", cell: (r) => r.answer_mode ?? "none" },
          { key: "s", head: "Status", cell: (r) => r.status },
          { key: "ft", head: "First token", align: "right", cell: (r) => fmtMs(r.first_token_ms) },
          { key: "tt", head: "Total", align: "right", cell: (r) => fmtMs(r.total_ms) },
          { key: "w", head: "Waiting", align: "right", cell: (r) => fmtMs(r.waiting_ms) },
          { key: "k", head: "Working", align: "right", cell: (r) => fmtMs(r.working_ms) },
          { key: "re", head: "Retrying", align: "right", cell: (r) => fmtMs(r.retrying_ms) },
          { key: "n", head: "Retries", align: "right", cell: (r) => r.retries },
          { key: "tk", head: "Tokens", align: "right", cell: (r) => (r.tokens_in + r.tokens_out).toLocaleString() },
        ]}
      />

      {request && <TraceDetail trace={trace.data} error={trace.error} onClose={() => onSelect(null)} />}
    </div>
  );
}
