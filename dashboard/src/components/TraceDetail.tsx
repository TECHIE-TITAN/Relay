import { useMemo } from "react";
import { useApi } from "../api";
import { Stage, buildLifecycle, explain } from "../lifecycle";
import { fmtMs } from "../stats";
import type { TraceEvent, TraceResponse } from "../types";
import { Lanes, Legend } from "./Lanes";
import { Notice } from "./ui";

interface ChunkInfo {
  chunk_id: string;
  missing?: boolean;
  paper?: string;
  pdf_pages?: [number, number];
  printed_pages?: [string | null, string | null];
  section?: string | null;
  text?: string;
  clipped?: boolean;
}

const KIND_COLOR: Record<string, string> = { wait: "var(--c2)", work: "var(--c1)", retry: "var(--c3)", mark: "var(--ink)" };
const KIND_NAME: Record<string, string> = { wait: "waiting", work: "working", retry: "retrying", mark: "moment" };

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-[7rem]">
      <div className="text-[18px] font-semibold leading-6 tracking-tight">{value}</div>
      <div className="text-[12px] text-ink2">{label}</div>
    </div>
  );
}

function range(a: number, b: number | null): string {
  return b === null ? `at ${fmtMs(a)}` : `${fmtMs(a)} to ${fmtMs(b)} (${fmtMs(b - a)})`;
}

/** The whole life of one request: why it was slow or failed, the stages in order with their timings, the passages
 *  it retrieved, the drafts that were checked and the answer that was released. All of it comes from the request's own events. */
export function TraceDetail({ trace, error, onClose }: { trace: TraceResponse | null; error: string | null; onClose: () => void }) {
  const events = trace?.events ?? [];
  const stages = useMemo(() => buildLifecycle(events), [trace]); // eslint-disable-line react-hooks/exhaustive-deps
  const why = useMemo(() => explain(trace?.row ?? null, events), [trace]); // eslint-disable-line react-hooks/exhaustive-deps
  const find = (name: string): TraceEvent | undefined => events.find((e) => e.event === name);
  const retrieval = find("retrieval");
  const hits: string[] = (retrieval?.hits as string[] | undefined) ?? [];
  const chunks = useApi<{ chunks: ChunkInfo[] }>(hits.length ? `/api/chunks?ids=${hits.join(",")}` : null);
  const verifies = events.filter((e) => e.event === "verify_result");
  const finalCheck = verifies.length ? verifies[verifies.length - 1] : undefined;
  const cited = new Set<string>(((finalCheck?.cited as string[] | undefined) ?? []).map(String));
  const done = find("done");
  const released = find("first_token");
  const ladder = find("ladder");
  const calls = events.filter((e) => e.event === "send").length;

  if (error) return <Notice tone="error">{error}</Notice>;
  if (!trace || !trace.row) return <p className="text-[13px] text-ink2">Loading the request.</p>;
  const row = trace.row;
  const horizon = Math.max(1, ...stages.map((s) => s.end ?? s.start));

  return (
    <section aria-label="Request detail" className="space-y-5 rounded-md border border-hair bg-surface p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-[16px] font-semibold tracking-tight">{row.question_id ?? "Request"}: {row.status}{row.answer_mode ? `, ${row.answer_mode} answer` : ""}</h3>
          <p className="text-[12px] text-ink2 tnum">{trace.request_id}</p>
        </div>
        <button className="rounded-sm border border-hair px-2.5 py-0.5 text-[13px] hover:bg-grid" onClick={onClose}>Close</button>
      </div>

      <div className="flex flex-wrap gap-x-8 gap-y-3 border-y border-hair py-3">
        <Fact label="total time" value={fmtMs(row.total_ms)} />
        <Fact label="first token (release)" value={fmtMs(row.first_token_ms)} />
        <Fact label="retries" value={String(row.retries)} />
        <Fact label="model calls" value={String(calls)} />
        <Fact label="tokens in / out" value={`${row.tokens_in.toLocaleString()} / ${row.tokens_out.toLocaleString()}`} />
        <Fact label="degradation ladder" value={ladder ? `level ${ladder.level}` : "not logged"} />
        <Fact label="verification" value={row.v0_passed === null || row.v0_passed === undefined ? "not run" : row.v0_passed ? "passed" : "failed"} />
        <Fact label="regenerated" value={row.regenerated ? "yes" : "no"} />
        <Fact label="retrieval setting" value={row.retrieval_setting ?? "not logged"} />
      </div>

      <div>
        <h4 className="mb-1 text-[14px] font-semibold tracking-tight">Why it took this long, or why it ended this way</h4>
        {why.length === 0 ? <p className="text-[13px] text-ink2">Nothing notable: no waiting, retries or failed checks were logged.</p> : (
          <ul className="list-disc space-y-0.5 pl-5 text-[13px]">{why.map((w, i) => <li key={i}>{w}</li>)}</ul>
        )}
      </div>

      <div>
        <h4 className="mb-1 text-[14px] font-semibold tracking-tight">Timing breakdown</h4>
        <Legend />
        <div className="mt-2"><Lanes rows={[row]} /></div>
      </div>

      <div>
        <h4 className="mb-1 text-[14px] font-semibold tracking-tight">Lifecycle, stage by stage</h4>
        <ul className="mb-2 flex flex-wrap gap-x-4 text-[13px] text-ink2" aria-label="Stage colours">
          {(["wait", "work", "retry"] as const).map((k) => <li key={k} className="flex items-center gap-1.5"><span className="inline-block h-2 w-5" style={{ background: KIND_COLOR[k] }} />{KIND_NAME[k]}</li>)}
          <li className="flex items-center gap-1.5"><span className="inline-block h-3 w-[2px]" style={{ background: KIND_COLOR.mark }} />a moment</li>
        </ul>
        <ol className="space-y-1.5">
          {stages.map((s: Stage) => (
            <li key={s.key} className="grid grid-cols-1 gap-x-4 md:grid-cols-[17rem_1fr]">
              <div className="min-w-0">
                <div className="text-[13px] font-medium">{s.label}</div>
                <div className="text-[12px] text-ink2 tnum">{range(s.start, s.end)}</div>
              </div>
              <div className="min-w-0">
                <div className="relative h-[10px] w-full bg-grid" aria-hidden>
                  {s.end === null ? (
                    <span className="absolute top-[-2px] h-[14px] w-[2px]" style={{ left: `calc(${(s.start / horizon) * 100}% - 1px)`, background: KIND_COLOR.mark }} />
                  ) : (
                    <span className="absolute top-0 h-full rounded-[2px]" style={{ left: `${(s.start / horizon) * 100}%`, width: `max(3px, ${((s.end - s.start) / horizon) * 100}%)`, background: KIND_COLOR[s.kind] }} />
                  )}
                </div>
                <ul className="mt-0.5 text-[12px] text-ink2">{s.lines.map((l, i) => <li key={i} className="break-words">{l}</li>)}</ul>
              </div>
            </li>
          ))}
        </ol>
      </div>

      {hits.length > 0 && (
        <div>
          <h4 className="mb-1 text-[14px] font-semibold tracking-tight">Retrieved passages</h4>
          <p className="mb-1 text-[12px] text-ink2">In the order the model saw them (S1, S2, ...). A passage is marked cited if the final verified answer cited it.</p>
          {chunks.error && <Notice tone="error">{chunks.error}</Notice>}
          <ol className="space-y-2">
            {hits.map((id, i) => {
              const c = chunks.data?.chunks.find((x) => x.chunk_id === id);
              return (
                <li key={id} className="rounded-sm border border-hair p-2 text-[13px]">
                  <div className="flex flex-wrap items-baseline gap-x-3">
                    <span className="font-semibold">S{i + 1}</span>
                    <span>{c && !c.missing ? `${c.paper}, pdf p.${c.pdf_pages?.[0] === c.pdf_pages?.[1] ? c.pdf_pages?.[0] : `${c.pdf_pages?.[0]}-${c.pdf_pages?.[1]}`}${c.printed_pages?.[0] ? ` (printed ${c.printed_pages[0]})` : ""}${c.section ? `, ${c.section}` : ""}` : id}</span>
                    {retrieval?.scores?.[i] !== undefined && <span className="text-ink2 tnum">score {retrieval.scores[i]}</span>}
                    {cited.has(`S${i + 1}`) && <span className="rounded-sm bg-ink px-1.5 text-[12px] text-surface">cited</span>}
                  </div>
                  {c?.missing && <p className="text-[12px] text-ink2">This passage is not in data/processed (the chunk files for this setting are missing).</p>}
                  {c && !c.missing && <p className="mt-1 break-words text-[12px] text-ink2">{c.text}{c.clipped ? " …" : ""}</p>}
                </li>
              );
            })}
          </ol>
        </div>
      )}

      {verifies.length > 0 && (
        <div>
          <h4 className="mb-1 text-[14px] font-semibold tracking-tight">Drafts that were checked</h4>
          <ol className="space-y-2">
            {verifies.map((v, i) => (
              <li key={i} className="rounded-sm border border-hair p-2 text-[13px]">
                <div className="font-medium">Attempt {String(v.attempt)}: {v.passed ? "passed" : `failed (${((v.failures ?? []) as string[]).join(", ")})`}{Number(v.attempt) === 3 ? " (the quoted-sentence fallback)" : ""}</div>
                {(v.details ?? []).length > 0 && <div className="text-[12px] text-ink2 break-words">{(v.details as string[]).join("; ")}</div>}
                <p className="mt-1 break-words text-[12px] text-ink2">{String(v.text ?? "")}</p>
              </li>
            ))}
          </ol>
        </div>
      )}

      {released && (
        <div>
          <h4 className="mb-1 text-[14px] font-semibold tracking-tight">What the person saw ({String(released.mode)})</h4>
          <p className="break-words rounded-sm border border-hair p-2 text-[13px]">{String(released.answer ?? "")}</p>
          {done && <p className="mt-1 text-[12px] text-ink2">Status {String(done.status)}; answer mode {String(done.answer_mode)}{done.v0_failures && (done.v0_failures as string[]).length ? `; final check failures ${(done.v0_failures as string[]).join(", ")}` : ""}.</p>}
        </div>
      )}

      <details>
        <summary className="cursor-pointer text-[14px] font-semibold tracking-tight">Raw event log ({events.length} events)</summary>
        <ol className="mt-1 max-h-72 overflow-y-auto text-[12px]">
          {events.map((e, i) => (
            <li key={i} className="grid grid-cols-[5rem_10rem_1fr] gap-2 border-b border-grid py-0.5">
              <span className="tnum text-ink2">{fmtMs((e.t_mono - (events[0]?.t_mono ?? 0)) * 1000)}</span>
              <span className="font-medium">{e.event}</span>
              <span className="break-words text-ink2">{JSON.stringify(Object.fromEntries(Object.entries(e).filter(([k]) => !["event", "t_mono", "request_id", "stage", "remaining_ms", "text", "answer"].includes(k)))).slice(0, 400)}</span>
            </li>
          ))}
        </ol>
      </details>
    </section>
  );
}
