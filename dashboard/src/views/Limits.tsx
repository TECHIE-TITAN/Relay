import { ReactNode } from "react";
import { useApi } from "../api";
import { Notice, Provenance } from "../components/ui";
import type { Ledger, Limits as L, RunInfo } from "../types";
import GatewayRun from "./GatewayRun";

function Row({ k, v, note }: { k: string; v: ReactNode; note?: string }) {
  return (
    <div className="grid grid-cols-[13rem_1fr] gap-3 py-1.5 border-b border-grid text-[13px]">
      <dt className="text-ink2">{k}</dt>
      <dd className="tnum">
        {v}
        {note && <span className="block text-[12px] text-ink2">{note}</span>}
      </dd>
    </div>
  );
}

function Block({ title, prov, children }: { title: string; prov: string | null | undefined; children: ReactNode }) {
  return (
    <section className="bg-surface border border-hair rounded-md p-4">
      <div className="flex items-center gap-2 mb-1">
        <h3 className="text-[15px] font-semibold tracking-tight">{title}</h3>
        <Provenance value={prov} />
      </div>
      {children}
    </section>
  );
}

/** Four kinds of number, kept apart: documented, observed (reported), observed (triggered), simulated. */
export default function Limits({ runs }: { runs: RunInfo[] | null }) {
  const lim = useApi<L>("/api/limits");
  const led = useApi<Ledger>("/api/ledger");
  if (lim.error) return <Notice tone="error">Could not read the limits: {lim.error}</Notice>;
  const d = lim.data;
  if (!d) return <p className="text-ink2">Loading.</p>;
  const doc = d.documented;
  const rep = d.observed_reported;
  const trig = d.observed_triggered;
  const sim = d.simulated;
  const l = led.data;

  return (
    <div className="space-y-5">
      <header>
        <h2 className="text-[22px] font-semibold tracking-tight">Limits</h2>
        <p className="text-[13px] text-ink2 max-w-[75ch]">
          What the provider documents, what it reported in its headers, what it returned when a limit was actually hit, and what the stand-in simulates. The badge says which kind each number is.
        </p>
      </header>

      <Block title={`Documented, model ${doc.model ?? ""}`} prov={doc.provenance}>
        <dl>
          <Row k="Requests per minute" v={doc.rpm} />
          <Row k="Tokens per minute" v={doc.tpm?.toLocaleString()} note="The binding limit: about 3.6 answers a minute at roughly 2,200 tokens each." />
          <Row k="Requests per day" v={doc.rpd?.toLocaleString()} />
          <Row k="Tokens per day" v={doc.tpd?.toLocaleString()} note="From the account console page. No response header carries it." />
        </dl>
      </Block>

      {rep ? (
        <Block title="Reported by the real service in its response headers" prov={rep.provenance}>
          <dl>
            <Row k="Requests per day" v={Number(rep.limit_requests_per_day).toLocaleString()} />
            <Row k="Tokens per minute" v={Number(rep.limit_tokens_per_minute).toLocaleString()} />
            <Row k="Real probe calls" v={Object.entries(d.probe_calls_by_stage).map(([k, v]) => `${k}: ${v}`).join(", ")} note="The probe spent about 12K tokens in total, all within the daily ledger." />
          </dl>
        </Block>
      ) : (
        <Notice>No real probe has been logged, so there are no reported limits to show.</Notice>
      )}

      {trig ? (
        <Block title="What the real service returned when a limit was hit" prov={trig.provenance}>
          <dl>
            <Row k="Status" v={`HTTP ${trig.status_code}`} note="Triggered on purpose once, at the per-minute request limit. The per-day limit was never triggered." />
            <Row k="Message" v={<span className="break-words">{trig.message}</span>} />
            <Row k="retry-after" v={trig.retry_after_s !== null && trig.retry_after_s !== undefined ? `${trig.retry_after_s} s` : "not measured"} />
            <Row k="Headers on that response" v={Object.entries(trig.headers ?? {}).map(([k, v]) => `${k}: ${v}`).join(", ") || "none"} note="Request-limit headers only: no token headers came back with the 429." />
          </dl>
        </Block>
      ) : (
        <Notice>No limit was triggered in the logged probe.</Notice>
      )}

      <Block title="Simulated by the stand-in" prov={sim.provenance}>
        <dl>
          <Row k="Default limits" v={`${sim.rpm} requests/min, ${sim.tpm?.toLocaleString()} tokens/min, ${sim.rpd?.toLocaleString()} requests/day, ${sim.tpd?.toLocaleString()} tokens/day`} note="Scenarios multiply these by limits_scale (5 in the load runs); each run's manifest records the factor." />
          <Row k="First-token time" v={`median ${sim.ttft_median_ms} ms`} note="Calibrated from the real probe." />
          <Row k="Decode speed" v={`${sim.tokens_per_s} tokens/s`} note="A choice: the real replies were too short to measure." />
          <Row k="Failures per call" v={`5xx ${(sim.p_5xx * 100).toFixed(1)}%, hang ${(sim.p_hang * 100).toFixed(1)}%, slow ${(sim.p_slow * 100).toFixed(1)}%, mid-stream drop ${(sim.p_midstream * 100).toFixed(1)}%`} note="Choices, as independent coin flips." />
        </dl>
        <p className="mt-2 text-[13px] text-ink2">{d.daily_limit_note}</p>
      </Block>

      <section className="bg-surface border border-hair rounded-md p-4">
        <h3 className="text-[15px] font-semibold tracking-tight mb-1">Today's client-side ledger</h3>
        {l?.available ? (
          <div className="space-y-2">
            <Gauge label="Tokens" used={l.tokens ?? 0} cap={l.token_cap ?? 1} />
            <Gauge label="Requests" used={l.requests ?? 0} cap={l.request_cap ?? 1} />
            <p className="text-[12px] text-ink2 max-w-[75ch]">
              Counts only calls made by this tool on {l.date} (UTC, an assumption: the provider's headers suggest the daily quota refills continuously). The cap is a safety margin below the provider's own limit.
            </p>
          </div>
        ) : (
          <p className="text-[13px] text-ink2">No ledger file yet: nothing has called the real service from this checkout.</p>
        )}
      </section>

      <GatewayRun runs={runs} />
    </div>
  );
}

function Gauge({ label, used, cap }: { label: string; used: number; cap: number }) {
  const pct = Math.min(100, (used / cap) * 100);
  return (
    <div>
      <div className="flex justify-between text-[13px]">
        <span>{label}</span>
        <span className="tnum">{used.toLocaleString()} of {cap.toLocaleString()} ({pct.toFixed(1)}%)</span>
      </div>
      <div className="mt-1 h-1.5 w-full bg-grid rounded-[2px]" role="meter" aria-valuenow={used} aria-valuemin={0} aria-valuemax={cap} aria-label={label}>
        <div className="h-full rounded-[2px] bg-ink" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
