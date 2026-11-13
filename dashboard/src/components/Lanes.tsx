import { useMemo, useState } from "react";
import type { RequestRow } from "../types";
import { fmtMs } from "../stats";

/** The one memorable element: each request is a thin lane split into waiting, working and retrying,
 *  with a notch where its first token was released. Everything is before the notch, because answers are
 *  buffered, verified and then released whole. Colour follows the entity: the same three hues everywhere. */
export const PARTS = [
  { key: "waiting_ms", label: "Waiting (queue, token window)", color: "var(--c2)" },
  { key: "working_ms", label: "Working (model call, retrieval, checks)", color: "var(--c1)" },
  { key: "retrying_ms", label: "Retrying (failed calls, back-off)", color: "var(--c3)" },
] as const;

export function Legend() {
  return (
    <ul className="flex flex-wrap gap-x-5 gap-y-1 text-[13px] text-ink2" aria-label="Legend">
      {PARTS.map((p) => (
        <li key={p.key} className="flex items-center gap-1.5">
          <span className="inline-block h-2 w-5 rounded-[1px]" style={{ background: p.color }} />
          {p.label}
        </li>
      ))}
      <li className="flex items-center gap-1.5">
        <span className="inline-block h-3 w-[2px]" style={{ background: "var(--ink)" }} />
        First token released
      </li>
    </ul>
  );
}

export function Lanes({
  rows,
  limit = 40,
  onPick,
  selected,
  sort = "recent",
}: {
  rows: RequestRow[];
  limit?: number;
  onPick?: (r: RequestRow) => void;
  selected?: string | null;
  sort?: "recent" | "slowest";
}) {
  const shown = useMemo(() => {
    const base = [...rows];
    if (sort === "slowest") base.sort((a, b) => b.total_ms - a.total_ms);
    else base.sort((a, b) => a.arrive_t - b.arrive_t);
    return sort === "slowest" ? base.slice(0, limit) : base.slice(-limit);
  }, [rows, limit, sort]);
  const max = useMemo(() => Math.max(1, ...shown.map((r) => r.total_ms)), [shown]);
  const [tip, setTip] = useState<{ r: RequestRow; x: number; y: number } | null>(null);

  if (rows.length === 0) return <p className="text-[13px] text-ink2">No finished requests yet.</p>;

  const pct = (ms: number) => (ms / max) * 100;
  return (
    <div className="relative">
      <div className="flex justify-between text-[12px] text-ink2 pl-[7.5rem] pr-1 pb-1" aria-hidden>
        <span>0</span>
        <span>{fmtMs(max / 2)}</span>
        <span>{fmtMs(max)}</span>
      </div>
      <ul className="space-y-[3px]">
        {shown.map((r) => {
          let x = 0;
          const segs = PARTS.map((p) => {
            const v = Math.max(0, r[p.key] as number);
            const seg = { p, left: pct(x), width: pct(v) };
            x += v;
            return seg;
          });
          const failed = r.status !== "ok";
          return (
            <li key={r.request_id}>
              <button
                className={`group flex w-full items-center gap-2 text-left rounded-sm ${selected === r.request_id ? "bg-grid" : "hover:bg-grid"}`}
                onClick={() => onPick?.(r)}
                onMouseMove={(e) => setTip({ r, x: e.clientX, y: e.clientY })}
                onMouseLeave={() => setTip(null)}
                onFocus={(e) => {
                  const b = e.currentTarget.getBoundingClientRect();
                  setTip({ r, x: b.left + 160, y: b.top });
                }}
                onBlur={() => setTip(null)}
                aria-label={`${r.question_id ?? r.request_id}: ${r.status}, total ${fmtMs(r.total_ms)}, waiting ${fmtMs(r.waiting_ms)}, working ${fmtMs(r.working_ms)}, retrying ${fmtMs(r.retrying_ms)}`}
              >
                <span className="w-[7.2rem] shrink-0 truncate text-[12px] text-ink2 tnum">
                  {r.question_id ?? r.request_id.slice(-4)}
                  {r.answer_mode ? ` ${r.answer_mode}` : ""}
                  {failed ? ` ${r.status}` : ""}
                </span>
                <span className="relative h-[9px] flex-1">
                  {segs.map((s) =>
                    s.width > 0 ? (
                      <span
                        key={s.p.key}
                        className="absolute top-0 h-full rounded-[2px]"
                        style={{ left: `${s.left}%`, width: `max(2px, calc(${s.width}% - 2px))`, background: s.p.color }}
                      />
                    ) : null,
                  )}
                  {r.first_token_ms !== null && (
                    <span
                      className="absolute -top-[2px] h-[13px] w-[2px]"
                      style={{ left: `calc(${pct(r.first_token_ms)}% - 1px)`, background: "var(--ink)" }}
                    />
                  )}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      {tip && (
        <div
          className="pointer-events-none fixed z-20 w-[16rem] rounded-sm border border-hair bg-surface p-2 text-[12px] shadow-sm"
          style={{ left: Math.min(tip.x + 14, window.innerWidth - 270), top: tip.y + 14 }}
        >
          <div className="font-semibold">
            {tip.r.question_id ?? "request"} <span className="font-normal text-ink2">{tip.r.status}{tip.r.answer_mode ? `, ${tip.r.answer_mode}` : ""}</span>
          </div>
          <dl className="mt-1 grid grid-cols-[1fr_auto] gap-x-3 tnum">
            <dt className="text-ink2">Total</dt><dd>{fmtMs(tip.r.total_ms)}</dd>
            <dt className="text-ink2">First token</dt><dd>{fmtMs(tip.r.first_token_ms)}</dd>
            <dt className="text-ink2">Waiting</dt><dd>{fmtMs(tip.r.waiting_ms)}</dd>
            <dt className="text-ink2">Working</dt><dd>{fmtMs(tip.r.working_ms)}</dd>
            <dt className="text-ink2">Retrying</dt><dd>{fmtMs(tip.r.retrying_ms)}</dd>
            <dt className="text-ink2">Unaccounted</dt><dd>{fmtMs(tip.r.unaccounted_ms)}</dd>
            <dt className="text-ink2">Retries</dt><dd>{tip.r.retries}</dd>
          </dl>
        </div>
      )}
    </div>
  );
}
