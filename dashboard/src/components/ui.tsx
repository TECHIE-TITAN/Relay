import { ReactNode, useState } from "react";
import { PROVENANCE, provenanceKey } from "../stats";

/** Provenance is shown by label AND by border style (solid, filled, dashed), never by colour alone. */
export function Provenance({ value }: { value: string | null | undefined }) {
  const key = provenanceKey(value);
  if (!key) return null;
  const p = PROVENANCE[key];
  const base = "inline-block rounded-sm px-1.5 py-px text-[12px] leading-5 whitespace-nowrap";
  const cls =
    p.style === "filled"
      ? `${base} bg-ink text-surface`
      : p.style === "dashed"
        ? `${base} border border-dashed border-ink2 text-ink2`
        : `${base} border border-ink text-ink`;
  return (
    <span className={cls} title={value ?? undefined}>
      {p.label}
    </span>
  );
}

export function SourceTag({ source }: { source: string }) {
  const real = source === "real";
  return (
    <span
      className={`inline-block rounded-sm px-1.5 text-[12px] leading-5 ${real ? "bg-ink text-surface" : "border border-dashed border-ink2 text-ink2"}`}
    >
      {real ? "Real service" : "Stand-in"}
    </span>
  );
}

export function Notice({ tone = "info", children }: { tone?: "info" | "warn" | "error"; children: ReactNode }) {
  const border = tone === "error" ? "border-critical" : tone === "warn" ? "border-c4" : "border-axis";
  return (
    <div role={tone === "error" ? "alert" : "status"} className={`border-l-2 ${border} bg-surface pl-3 pr-2 py-2 text-[13px] text-ink2`}>
      {children}
    </div>
  );
}

/** A chart with its table twin: every value is reachable without hover. */
export function ChartCard({
  title,
  note,
  chart,
  table,
}: {
  title: string;
  note?: ReactNode;
  chart: ReactNode;
  table: ReactNode;
}) {
  const [view, setView] = useState<"chart" | "table">("chart");
  return (
    <section className="bg-surface border border-hair rounded-md p-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="text-[15px] font-semibold tracking-tight">{title}</h3>
          {note && <p className="mt-0.5 text-[13px] text-ink2 max-w-[70ch]">{note}</p>}
        </div>
        <div role="group" aria-label="View" className="flex shrink-0 border border-hair rounded-sm overflow-hidden text-[13px]">
          {(["chart", "table"] as const).map((v) => (
            <button
              key={v}
              aria-pressed={view === v}
              onClick={() => setView(v)}
              className={`px-2.5 py-0.5 ${view === v ? "bg-ink text-surface" : "text-ink2 hover:bg-grid"}`}
            >
              {v === "chart" ? "Chart" : "Table"}
            </button>
          ))}
        </div>
      </div>
      <div className="mt-3">{view === "chart" ? chart : table}</div>
    </section>
  );
}

export interface Col<T> {
  key: string;
  head: ReactNode;
  cell: (row: T) => ReactNode;
  align?: "left" | "right";
  title?: string;
}

export function DataTable<T>({ cols, rows, rowKey, onRow, selected }: {
  cols: Col<T>[];
  rows: T[];
  rowKey: (r: T) => string;
  onRow?: (r: T) => void;
  selected?: string | null;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[13px] border-collapse">
        <thead>
          <tr className="text-left text-ink2">
            {cols.map((c) => (
              <th key={c.key} title={c.title} className={`font-medium py-1.5 pr-3 border-b border-axis ${c.align === "right" ? "text-right" : ""}`}>
                {c.head}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const id = rowKey(r);
            return (
              <tr
                key={id}
                onClick={onRow ? () => onRow(r) : undefined}
                onKeyDown={onRow ? (e) => (e.key === "Enter" || e.key === " ") && onRow(r) : undefined}
                tabIndex={onRow ? 0 : undefined}
                className={`border-b border-grid ${onRow ? "cursor-pointer hover:bg-grid" : ""} ${selected === id ? "bg-grid" : ""}`}
              >
                {cols.map((c) => (
                  <td key={c.key} className={`py-1.5 pr-3 align-top tnum ${c.align === "right" ? "text-right" : ""}`}>
                    {c.cell(r)}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function Figure({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="min-w-[8rem]" title={hint}>
      <div className="text-[24px] leading-7 font-semibold tracking-tight">{value}</div>
      <div className="text-[13px] text-ink2">{label}</div>
    </div>
  );
}
