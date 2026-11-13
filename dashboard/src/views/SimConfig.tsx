import { useState } from "react";
import type { ReactNode } from "react";
import { Notice, Provenance } from "../components/ui";
import { DEFAULT_SPEC, PAIRS, SimOptions, SimSpec, capacityPerMin, expectedRequests, validate } from "../sim";

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block text-[13px]">
      <span className="font-medium">{label}</span>
      {hint && <span className="ml-2 text-[12px] text-ink2">{hint}</span>}
      <div className="mt-1">{children}</div>
    </label>
  );
}

const input = "w-full rounded-sm border border-hair bg-surface px-2 py-1 tnum";

/** The form offers only what the runner can express (the server checks the same limits again). */
export default function SimConfig({ options, busy, error, running, onStart, onPair }: {
  options: SimOptions;
  busy: boolean;
  error: string | null;
  running: number;
  onStart: (spec: SimSpec) => void;
  onPair: (kindId: string, spec: SimSpec) => void;
}) {
  const [spec, setSpec] = useState<SimSpec>(DEFAULT_SPEC);
  const [pair, setPair] = useState(PAIRS[0].id);
  const set = (p: Partial<SimSpec>) => setSpec((s) => ({ ...s, ...p }));
  const setW = (p: Partial<SimSpec["whatif"]>) => setSpec((s) => ({ ...s, whatif: { ...s.whatif, ...p } }));
  const lim = options.limits;
  const cap = capacityPerMin(options, spec.limits_scale);
  const expected = expectedRequests(spec, cap);
  const problem = validate(spec);
  const full = running >= options.max_running;
  const kind = PAIRS.find((p) => p.id === pair) ?? PAIRS[0];
  const slow = spec.whatif.slow_p > 0;
  const halve = spec.whatif.halve_limits_at_s !== null;

  return (
    <section aria-label="Configure a simulation" className="rounded-md border border-hair bg-surface p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-[16px] font-semibold tracking-tight">Configure a simulation</h3>
        <Provenance value="SIMULATED" />
      </div>
      <p className="mt-0.5 max-w-[80ch] text-[13px] text-ink2">{options.provenance} It runs the same pipeline and gateway as every other run, against the stand-in model server, and writes an ordinary run you can open under Traces.</p>

      <div className="mt-3 grid gap-x-6 gap-y-4 md:grid-cols-2 xl:grid-cols-3">
        <div className="md:col-span-2 xl:col-span-3">
          <span className="text-[13px] font-medium">Load pattern</span>
          <div role="group" aria-label="Load pattern" className="mt-1 flex flex-wrap overflow-hidden rounded-sm border border-hair w-fit">
            {options.patterns.map((p) => (
              <button key={p.id} aria-pressed={spec.pattern === p.id} title={p.description} onClick={() => set({ pattern: p.id, rate_x: p.id === "sustained_overload" ? 2.5 : spec.pattern === "sustained_overload" ? 0.7 : spec.rate_x })}
                className={`px-3 py-1 text-[13px] ${spec.pattern === p.id ? "bg-ink text-surface" : "text-ink2 hover:bg-grid"}`}>
                {p.label}
              </button>
            ))}
          </div>
          <p className="mt-1 text-[12px] text-ink2">{options.patterns.find((p) => p.id === spec.pattern)?.description}</p>
        </div>

        <Field label="Request rate" hint={`times capacity (${lim.rate_x.min} to ${lim.rate_x.max})`}>
          <input type="range" min={lim.rate_x.min} max={lim.rate_x.max} step={0.05} value={spec.rate_x} onChange={(e) => set({ rate_x: Number(e.target.value) })} className="w-full" aria-label="Request rate in times capacity" />
          <div className="flex justify-between text-[12px] text-ink2 tnum">
            <span>{spec.rate_x.toFixed(2)} x capacity</span>
            <span>{cap === null ? "" : `about ${(spec.rate_x * cap).toFixed(1)} requests a minute`}</span>
          </div>
        </Field>

        {spec.pattern === "burst" && (
          <Field label="Burst rate" hint={`times capacity (${lim.burst_x.min} to ${lim.burst_x.max})`}>
            <input type="range" min={lim.burst_x.min} max={lim.burst_x.max} step={0.5} value={spec.burst_x} onChange={(e) => set({ burst_x: Number(e.target.value) })} className="w-full" aria-label="Burst rate in times capacity" />
            <div className="text-[12px] text-ink2 tnum">{spec.burst_x.toFixed(1)} x capacity for about 15% of the run</div>
          </Field>
        )}

        <Field label="Duration" hint="seconds">
          <input type="number" min={lim.duration_s.min} max={lim.duration_s.max} step={10} value={spec.duration_s} onChange={(e) => set({ duration_s: Number(e.target.value) })} className={input} />
        </Field>

        <Field label="Target concurrency" hint="requests in the pipeline at once">
          <input type="number" min={lim.concurrency.min} max={lim.concurrency.max} value={spec.concurrency} disabled={spec.whatif.retry_policy === "naive"} onChange={(e) => set({ concurrency: Number(e.target.value) })} className={input} />
          {spec.whatif.retry_policy === "naive" && <div className="text-[12px] text-ink2">The naive policy has no concurrency cap, as in the retry_naive scenario.</div>}
        </Field>

        <Field label="Limits scale" hint="SIMULATED: the documented limits times this">
          <select value={spec.limits_scale} onChange={(e) => set({ limits_scale: Number(e.target.value) })} className={input}>
            {[1, 2, 5, 10].map((v) => <option key={v} value={v}>{v}{v === 1 ? " (the documented limits)" : v === 5 ? " (used by the load scenarios)" : ""}</option>)}
          </select>
          <div className="text-[12px] text-ink2 tnum">{cap === null ? "" : `capacity ${cap.toFixed(1)} answers a minute (arithmetic on the limits, not a measurement)`}</div>
        </Field>

        <Field label="Patience" hint="seconds before a request is given up">
          <input type="number" min={lim.deadline_s.min} max={lim.deadline_s.max} value={spec.deadline_s} onChange={(e) => set({ deadline_s: Number(e.target.value) })} className={input} />
        </Field>

        <Field label="Seed" hint="same seed, same arrival times">
          <input type="number" min={0} max={1000000} value={spec.seed} onChange={(e) => set({ seed: Number(e.target.value) })} className={input} />
        </Field>
      </div>

      <fieldset className="mt-4 rounded-sm border border-hair p-3">
        <legend className="px-1 text-[13px] font-medium">What if</legend>
        <div className="grid gap-x-6 gap-y-3 md:grid-cols-3">
          <div className="text-[13px]">
            <label className="flex items-center gap-2"><input type="checkbox" checked={halve} onChange={(e) => setW({ halve_limits_at_s: e.target.checked ? Math.max(10, Math.round(spec.duration_s / 2)) : null })} />Halve the RPM and TPM mid-run</label>
            {halve && (
              <div className="mt-1 flex items-center gap-2 text-[12px] text-ink2">at second
                <input type="number" min={lim.halve_limits_at_s.min} max={Math.min(lim.halve_limits_at_s.max, spec.duration_s - 1)} value={spec.whatif.halve_limits_at_s ?? 10} onChange={(e) => setW({ halve_limits_at_s: Number(e.target.value) })} className="w-20 rounded-sm border border-hair bg-surface px-1.5 py-0.5 tnum" aria-label="Second at which the limits are halved" />
              </div>
            )}
            <div className="mt-0.5 text-[12px] text-ink2">The stand-in changes its limits without telling the client.</div>
          </div>
          <div className="text-[13px]">
            <label className="flex items-center gap-2"><input type="checkbox" checked={slow} onChange={(e) => setW({ slow_p: e.target.checked ? 0.15 : 0 })} />Inject slow replies</label>
            {slow && (
              <div className="mt-1 space-y-1 text-[12px] text-ink2 tnum">
                <div>{(spec.whatif.slow_p * 100).toFixed(0)}% of calls
                  <input type="range" min={0.01} max={lim.slow_p.max} step={0.01} value={spec.whatif.slow_p} onChange={(e) => setW({ slow_p: Number(e.target.value) })} className="ml-2 align-middle" aria-label="Share of slow replies" />
                </div>
                <div>{spec.whatif.slow_factor.toFixed(0)} times slower
                  <input type="range" min={lim.slow_factor.min} max={lim.slow_factor.max} step={1} value={spec.whatif.slow_factor} onChange={(e) => setW({ slow_factor: Number(e.target.value) })} className="ml-2 align-middle" aria-label="How much slower a slow reply is" />
                </div>
              </div>
            )}
            <div className="mt-0.5 text-[12px] text-ink2">Slow successes are a stand-in choice (SIMULATED).</div>
          </div>
          <div className="text-[13px]">
            <span className="font-medium">Retry policy</span>
            <div role="group" aria-label="Retry policy" className="mt-1 flex w-fit overflow-hidden rounded-sm border border-hair">
              {options.retry_policies.map((p) => (
                <button key={p.id} aria-pressed={spec.whatif.retry_policy === p.id} onClick={() => setW({ retry_policy: p.id })} title={p.label} className={`px-3 py-1 ${spec.whatif.retry_policy === p.id ? "bg-ink text-surface" : "text-ink2 hover:bg-grid"}`}>
                  {p.id === "budgeted" ? "Budgeted" : "Naive"}
                </button>
              ))}
            </div>
          </div>
        </div>
      </fieldset>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button disabled={busy || full || problem !== null} onClick={() => onStart(spec)} className="rounded-sm bg-ink px-4 py-1.5 text-[14px] font-semibold text-surface disabled:opacity-40">
          Start simulation
        </button>
        <span className="text-[13px] text-ink2 tnum">{expected === null ? "" : `about ${expected} requests offered over ${spec.duration_s} s`}</span>
        {full && <span className="text-[13px] text-ink2">{options.max_running} are already running: stop one or wait.</span>}
      </div>
      {problem && <p className="mt-2 text-[13px]" role="alert">{problem}</p>}
      {error && <div className="mt-2"><Notice tone="error">{error}</Notice></div>}

      <div className="mt-4 border-t border-hair pt-3">
        <h4 className="text-[14px] font-semibold">Compare two runs</h4>
        <p className="mt-0.5 max-w-[80ch] text-[12px] text-ink2">Starts two runs at once that differ in one thing and share the seed, so the same arrival times hit both. They share this machine's CPU, so absolute timings are comparable between them, not with other runs.</p>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <select value={pair} onChange={(e) => setPair(e.target.value)} className="rounded-sm border border-hair bg-surface px-2 py-1 text-[13px]" aria-label="What to compare">
            {PAIRS.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
          </select>
          <button disabled={busy || running > 0 || options.max_running < 2 || problem !== null} onClick={() => onPair(pair, spec)} className="rounded-sm border border-ink px-3 py-1 text-[13px] font-semibold disabled:opacity-40">
            Start both: {kind.names[0]} and {kind.names[1]}
          </button>
          {running > 0 && <span className="text-[12px] text-ink2">Needs both slots free.</span>}
        </div>
        <p className="mt-1 max-w-[80ch] text-[12px] text-ink2">{kind.explain}</p>
      </div>
    </section>
  );
}
