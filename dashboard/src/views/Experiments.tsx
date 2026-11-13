import { useMemo } from "react";
import { Bar, BarChart, CartesianGrid, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useApi } from "../api";
import { ChartCard, DataTable, Notice, Provenance, SourceTag } from "../components/ui";
import { fmtMs, fmtNum, fmtPct, isRealScenario } from "../stats";
import type { Experiments as Exp, ScenarioStats } from "../types";

const SETTINGS = ["S1", "S2", "S3", "S4"] as const;
const REAL_SETTINGS = ["S1", "S2", "S3"] as const;
const STAND_IN_ORDER = ["smoke", "steady", "burst", "sustained_overload", "limit_halved", "limit_halved_hot", "daily_exhausted", "retry_naive", "retry_budgeted", "retry_naive_hot", "retry_budgeted_hot"];
const GRADES = [
  { key: "correct", label: "Correct and cited", color: "var(--c1)" },
  { key: "wrong", label: "Wrong", color: "var(--c2)" },
  { key: "refused_correctly", label: "Refused correctly", color: "var(--c3)" },
  { key: "refused_wrongly", label: "Refused wrongly", color: "var(--c4)" },
] as const;

const axis = { stroke: "var(--axis)", tick: { fill: "var(--ink2)", fontSize: 12 }, tickLine: false } as const;
const tip = { contentStyle: { background: "var(--surface)", border: "1px solid var(--hair)", fontSize: 12 }, cursor: { fill: "var(--grid)" } } as const;
const Key = ({ items }: { items: { color: string; label: string }[] }) => (
  <ul className="mt-1 flex flex-wrap gap-x-4 text-[13px] text-ink2" aria-label="Legend">
    {items.map((i) => <li key={i.label} className="flex items-center gap-1.5"><span className="inline-block h-2 w-5" style={{ background: i.color }} />{i.label}</li>)}
  </ul>
);
const NM = "not measured";

function settingText(cfg: NonNullable<Exp["settings"]>[string] | undefined): string {
  if (!cfg) return "";
  const rerank = !cfg.rerank || cfg.rerank === "none" ? "no re-rank" : `re-rank ${cfg.rerank} (fetch ${cfg.fetch_k})`;
  return `${cfg.chunk_tokens}-token pieces, ${cfg.k} fetched, ${rerank}`;
}

function cost(v: number | null | undefined): string {
  return v === null || v === undefined ? NM : v.toFixed(4);
}

function sumKeys(d: Record<string, number> | undefined, prefix: string, detail: string): number {
  return d?.[`${prefix}:${detail}`] ?? 0;
}

export default function Experiments() {
  const { data, error } = useApi<Exp>("/api/experiments");
  const sc = data?.scenarios ?? {};

  const quality = useMemo(
    () => REAL_SETTINGS.map((k) => ({ key: k, s: sc[`quality_${k}`] as ScenarioStats | undefined, ttft: data?.model_ttft?.[`quality_${k}`], detail: data?.grade_detail?.[`quality_${k}`] })),
    [data], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const standIn = useMemo(
    () => Object.keys(sc).filter((n) => !isRealScenario(n)).sort((a, b) => (STAND_IN_ORDER.indexOf(a) < 0 ? 99 : STAND_IN_ORDER.indexOf(a)) - (STAND_IN_ORDER.indexOf(b) < 0 ? 99 : STAND_IN_ORDER.indexOf(b))).map((n) => ({ name: n, s: sc[n] })),
    [data], // eslint-disable-line react-hooks/exhaustive-deps
  );

  if (error) return <Notice tone="error">Could not read the experiments: {error}</Notice>;
  if (!data) return <p className="text-ink2">Loading.</p>;
  if (!data.available) return <Notice>No merged results yet. {data.reason} Run <code>python -m relay.cli analyze</code>, then reload.</Notice>;

  const graded = quality.filter((q) => q.s && q.s.n_graded > 0);
  const stackData = graded.map((q) => ({ name: q.key, n: q.s!.n_graded, ...{ correct: q.s!.grades.correct ?? 0, wrong: q.s!.grades.wrong ?? 0, refused_correctly: q.s!.grades.refused_correctly ?? 0, refused_wrongly: q.s!.grades.refused_wrongly ?? 0 } }));
  const tokenData = quality.filter((q) => q.s).map((q) => ({ name: q.key, all: q.s!.tokens_per_answer === null ? 0 : Math.round(q.s!.tokens_per_answer), model: q.s!.tokens_per_model_answer === null ? 0 : Math.round(q.s!.tokens_per_model_answer) }));
  const timeData = quality.filter((q) => q.s).map((q) => ({ name: q.key, mt50: q.ttft?.p50 ?? 0, mt95: q.ttft?.p95 ?? 0, ft50: q.s!.latency.ft.p50 ?? 0, ft95: q.s!.latency.ft.p95 ?? 0 }));
  const retr = data.retrieval?.settings ?? {};
  const retrRows = SETTINGS.map((k) => {
    const r = retr[k];
    const real = quality.find((q) => q.key === k);
    return { key: k, r, real, desc: settingText(data.settings?.[k]) };
  });
  const recallData = retrRows.filter((x) => x.r).map((x) => ({ name: x.key === "S4" ? "S4 (retrieval only)" : x.key, recall: Number((x.r!.recall_sum / Math.max(1, x.r!.answerable)).toFixed(2)) }));
  const standLatency = standIn.filter((x) => x.s.latency.ft.p50 !== null && x.s.latency.ft.p95 !== null).map((x) => ({ name: x.name, p50: x.s.latency.ft.p50 as number, p95: x.s.latency.ft.p95 as number }));

  return (
    <div className="space-y-8">
      <header>
        <h2 className="text-[22px] font-semibold tracking-tight">Experiments</h2>
        <p className="max-w-[78ch] text-[13px] text-ink2">
          Every figure is computed from the merged <code>results.csv</code> ({data.rows} requests), the runs' own event logs and the saved offline retrieval evaluation. The three retrieval settings S1 to S3 ran on the real service (20 questions each, graded by hand); S4 was scored on retrieval only.
        </p>
      </header>

      <section className="space-y-4" aria-label="Answer quality">
        <h3 className="text-[17px] font-semibold tracking-tight">Answer quality: S1, S2 and S3</h3>
        <ChartCard
          title="Correct answers, wrong answers and refusals"
          note="The same 20 questions, 5 of them unanswerable. Each bar is all 20 questions. A partial answer counts as wrong here."
          chart={
            graded.length === 0 ? <p className="text-[13px] text-ink2">No graded run yet. Grade a quality run with grade-sheet and apply-grades, then run analyze.</p> : (
              <div className="h-44" role="img" aria-label="Share of questions correct, wrong, refused correctly and refused wrongly for each setting">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={stackData} layout="vertical" stackOffset="expand" margin={{ top: 4, right: 12, bottom: 0, left: 8 }} barCategoryGap={10}>
                    <XAxis type="number" tickFormatter={(v) => `${Math.round(v * 100)}%`} {...axis} />
                    <YAxis type="category" dataKey="name" width={36} {...axis} />
                    <Tooltip {...tip} />
                    {GRADES.map((g) => (
                      <Bar key={g.key} dataKey={g.key} name={g.label} stackId="q" fill={g.color} stroke="var(--surface)" strokeWidth={2} isAnimationActive={false}
                        label={(p: any) => (p.value && p.width > 22 ? <text x={p.x + p.width / 2} y={p.y + p.height / 2} textAnchor="middle" dominantBaseline="central" fontSize={12} fontWeight={600} fill={g.key === "refused_wrongly" ? "#0b0b0b" : "#ffffff"}>{p.value}</text> : <g />)} />
                    ))}
                  </BarChart>
                </ResponsiveContainer>
                <Key items={GRADES.map((g) => ({ color: g.color, label: g.label }))} />
              </div>
            )
          }
          table={
            <DataTable rows={quality} rowKey={(q) => q.key} cols={[
              { key: "k", head: "Setting", cell: (q) => `${q.key}: ${settingText(data.settings?.[q.key])}` },
              { key: "n", head: "Graded", align: "right", cell: (q) => q.s?.n_graded ?? "not run" },
              ...GRADES.map((g) => ({ key: g.key, head: g.label, align: "right" as const, cell: (q: (typeof quality)[number]) => (q.s && q.s.n_graded ? `${q.s.grades[g.key] ?? 0} (${fmtPct(q.s.grades[g.key] ?? 0, q.s.n_graded)})` : NM) })),
            ]} />
          }
        />
        <div>
          <h4 className="mb-1 text-[14px] font-semibold tracking-tight">Where the answers came from</h4>
          <p className="mb-1 max-w-[78ch] text-[12px] text-ink2">Each answer was a model answer, a quoted sentence (the fallback when the checks reject the model's drafts) or a refusal. A quoted sentence is graded on its text like any other answer.</p>
          <DataTable rows={quality} rowKey={(q) => q.key} cols={[
            { key: "k", head: "Setting", cell: (q) => q.key },
            { key: "ml", head: "Model: correct / partial / incorrect", align: "right", cell: (q) => (q.detail ? `${sumKeys(q.detail, "llm", "correct")} / ${sumKeys(q.detail, "llm", "partial")} / ${sumKeys(q.detail, "llm", "incorrect")}` : NM) },
            { key: "ex", head: "Quoted sentence: correct / partial / incorrect", align: "right", cell: (q) => (q.detail ? `${sumKeys(q.detail, "extractive", "correct")} / ${sumKeys(q.detail, "extractive", "partial")} / ${sumKeys(q.detail, "extractive", "incorrect")}` : NM) },
            { key: "rf", head: "Refusals: right / wrong", align: "right", cell: (q) => (q.detail ? `${sumKeys(q.detail, "refusal", "correct_refusal")} / ${sumKeys(q.detail, "refusal", "wrong_refusal")}` : NM) },
          ]} />
          <p className="mt-1 text-[12px] text-ink2">Grades were proposed by the assistant and applied by the author; the split comes from each run's grades.csv.</p>
        </div>
      </section>

      <section className="space-y-4" aria-label="Cost and speed">
        <h3 className="text-[17px] font-semibold tracking-tight">What each setting costs and how fast it is</h3>
        <div className="grid gap-4 lg:grid-cols-2">
          <ChartCard
            title="Tokens per answer"
            note="Provider-reported tokens. 'All answers' counts a free refusal as zero; 'model answers' counts only the questions that reached the model."
            chart={
              <div className="h-48" role="img" aria-label="Tokens per answer for each setting">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={tokenData} margin={{ top: 14, right: 8, bottom: 0, left: 0 }} barGap={2}>
                    <CartesianGrid stroke="var(--grid)" vertical={false} />
                    <XAxis dataKey="name" {...axis} />
                    <YAxis {...axis} width={44} />
                    <Tooltip {...tip} formatter={(v: any) => `${Number(v).toLocaleString()} tokens`} />
                    <Bar dataKey="all" name="All answers" fill="var(--c1)" radius={[3, 3, 0, 0]} isAnimationActive={false}><LabelList dataKey="all" position="top" style={{ fill: "var(--ink2)", fontSize: 11 }} /></Bar>
                    <Bar dataKey="model" name="Model answers only" fill="var(--c2)" radius={[3, 3, 0, 0]} isAnimationActive={false}><LabelList dataKey="model" position="top" style={{ fill: "var(--ink2)", fontSize: 11 }} /></Bar>
                  </BarChart>
                </ResponsiveContainer>
                <Key items={[{ color: "var(--c1)", label: "All answers" }, { color: "var(--c2)", label: "Model answers only" }]} />
              </div>
            }
            table={<DataTable rows={quality} rowKey={(q) => q.key} cols={[
              { key: "k", head: "Setting", cell: (q) => q.key },
              { key: "a", head: "All answers", align: "right", cell: (q) => fmtNum(q.s?.tokens_per_answer ?? null, 0) },
              { key: "m", head: "Model answers only", align: "right", cell: (q) => fmtNum(q.s?.tokens_per_model_answer ?? null, 0) },
              { key: "c", head: "Cost per 1,000 answers (notional USD)", align: "right", cell: (q) => cost(q.s?.cost_per_1000) },
            ]} />}
          />
          <ChartCard
            title="Time to first token: the model, and the person"
            note="Log scale. The model's own time to first token is under 2 s. What the person waited includes queueing for the token window, because the 20 questions were sent back to back."
            chart={
              <div className="h-48" role="img" aria-label="Model-call time to first token and the first token the person saw, median and 95th percentile, per setting">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={timeData} margin={{ top: 4, right: 8, bottom: 0, left: 0 }} barGap={2}>
                    <CartesianGrid stroke="var(--grid)" vertical={false} />
                    <XAxis dataKey="name" {...axis} />
                    <YAxis scale="log" domain={[100, "auto"]} allowDataOverflow {...axis} width={52} tickFormatter={(v) => fmtMs(v)} />
                    <Tooltip {...tip} formatter={(v: any) => fmtMs(Number(v))} />
                    <Bar dataKey="mt50" name="Model call, median" fill="var(--c1)" isAnimationActive={false} />
                    <Bar dataKey="mt95" name="Model call, 95th percentile" fill="var(--c2)" isAnimationActive={false} />
                    <Bar dataKey="ft50" name="What the person waited, median" fill="var(--c3)" isAnimationActive={false} />
                    <Bar dataKey="ft95" name="What the person waited, 95th percentile" fill="var(--c4)" isAnimationActive={false} />
                  </BarChart>
                </ResponsiveContainer>
                <Key items={[{ color: "var(--c1)", label: "Model call, median" }, { color: "var(--c2)", label: "Model call, 95th" }, { color: "var(--c3)", label: "Person waited, median" }, { color: "var(--c4)", label: "Person waited, 95th" }]} />
              </div>
            }
            table={<DataTable rows={quality} rowKey={(q) => q.key} cols={[
              { key: "k", head: "Setting", cell: (q) => q.key },
              { key: "m5", head: "Model call p50", align: "right", cell: (q) => fmtMs(q.ttft?.p50) },
              { key: "m9", head: "p95", align: "right", cell: (q) => fmtMs(q.ttft?.p95) },
              { key: "f5", head: "First token p50", align: "right", cell: (q) => fmtMs(q.s?.latency.ft.p50) },
              { key: "f9", head: "p95", align: "right", cell: (q) => fmtMs(q.s?.latency.ft.p95) },
              { key: "t9", head: "Total time p95", align: "right", cell: (q) => fmtMs(q.s?.latency.total.p95) },
            ]} />}
          />
        </div>
      </section>

      <section className="space-y-4" aria-label="Retrieval">
        <h3 className="text-[17px] font-semibold tracking-tight">Retrieval performance, all four settings</h3>
        <ChartCard
          title="How much of the evidence each setting retrieved"
          note={data.retrieval ? `Mean share of a question's gold evidence found in the retrieved pieces, over the answerable questions. Offline and free: no model call. From ${data.retrieval.file}.` : "No saved retrieval evaluation was found."}
          chart={
            recallData.length === 0 ? <p className="text-[13px] text-ink2">No retrieval evaluation saved.</p> : (
              <div className="h-44" role="img" aria-label="Mean evidence recall for each retrieval setting">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={recallData} margin={{ top: 14, right: 8, bottom: 0, left: 0 }}>
                    <CartesianGrid stroke="var(--grid)" vertical={false} />
                    <XAxis dataKey="name" {...axis} />
                    <YAxis domain={[0, 1]} {...axis} width={32} />
                    <Tooltip {...tip} />
                    <Bar dataKey="recall" name="Mean evidence recall" fill="var(--c1)" radius={[3, 3, 0, 0]} barSize={40} isAnimationActive={false}><LabelList dataKey="recall" position="top" style={{ fill: "var(--ink2)", fontSize: 12 }} /></Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )
          }
          table={<p className="text-[13px] text-ink2">The values are in the table below.</p>}
        />
        <DataTable rows={retrRows} rowKey={(x) => x.key} cols={[
          { key: "k", head: "Setting", cell: (x) => <span><strong>{x.key}</strong> {x.desc}</span> },
          { key: "rc", head: "Mean evidence recall", align: "right", cell: (x) => (x.r ? (x.r.recall_sum / Math.max(1, x.r.answerable)).toFixed(2) : NM) },
          { key: "ha", head: "All evidence retrieved", align: "right", cell: (x) => (x.r ? `${x.r.hit_all} of ${x.r.answerable}` : NM) },
          { key: "ab", head: "Answerable wrongly abstained", align: "right", cell: (x) => (x.r ? x.r.answerable_abstained : NM) },
          { key: "un", head: "Unanswerable reaching the model", align: "right", cell: (x) => (x.r ? x.r.unanswerable_reach_model : NM) },
          { key: "cx", head: "Context tokens per question (approx.)", align: "right", cell: (x) => (x.r ? Math.round(x.r.ctx / Math.max(1, x.r.reach)).toLocaleString() : NM) },
          { key: "q", head: "Correct on the real model", align: "right", cell: (x) => (x.real?.s && x.real.s.n_graded ? fmtPct(x.real.s.grades.correct ?? 0, x.real.s.n_graded) : NM) },
          { key: "tk", head: "Tokens per answer", align: "right", cell: (x) => (x.real?.s ? fmtNum(x.real.s.tokens_per_answer, 0) : NM) },
          { key: "tt", head: "Model-call TTFT p50", align: "right", cell: (x) => (x.real?.ttft ? fmtMs(x.real.ttft.p50) : NM) },
        ]} />
        <Notice>
          S4 (1024-token pieces, 3 fetched) was scored on retrieval only, so its answer quality, tokens per answer and time to first token are <strong>not measured</strong>. Context tokens are an approximation (characters divided by 4), not provider counts.
        </Notice>
      </section>

      <section className="space-y-4" aria-label="Stand-in load scenarios">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-[17px] font-semibold tracking-tight">Load scenarios on the stand-in</h3>
          <Provenance value="SIMULATED" />
        </div>
        <p className="max-w-[78ch] text-[12px] text-ink2">The stand-in's limits are the documented limits times 5 and its latency is calibrated from the real probe; its token counts are simulated. These runs test how the pipeline behaves under load, not how fast the real service is.</p>
        <ChartCard
          title="First token by scenario, median and 95th percentile"
          note="Log scale. The latest run of each scenario."
          chart={
            <div style={{ height: Math.max(180, standLatency.length * 28 + 50) }} role="img" aria-label="Median and 95th percentile first-token time per stand-in scenario, log scale">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={standLatency} layout="vertical" margin={{ top: 4, right: 56, bottom: 0, left: 8 }} barCategoryGap={6} barGap={2}>
                  <CartesianGrid stroke="var(--grid)" horizontal={false} />
                  <XAxis type="number" scale="log" domain={[1, "auto"]} allowDataOverflow tickFormatter={(v) => fmtMs(v)} {...axis} />
                  <YAxis type="category" dataKey="name" width={130} {...axis} />
                  <Tooltip {...tip} formatter={(v: any) => fmtMs(Number(v))} />
                  <Bar dataKey="p50" name="Median" fill="var(--c1)" radius={[0, 3, 3, 0]} barSize={9} isAnimationActive={false} />
                  <Bar dataKey="p95" name="95th percentile" fill="var(--c2)" radius={[0, 3, 3, 0]} barSize={9} isAnimationActive={false}>
                    <LabelList dataKey="p95" position="right" formatter={(v: any) => fmtMs(Number(v))} style={{ fill: "var(--ink2)", fontSize: 11 }} />
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
              <Key items={[{ color: "var(--c1)", label: "Median" }, { color: "var(--c2)", label: "95th percentile" }]} />
            </div>
          }
          table={<DataTable rows={standIn} rowKey={(x) => x.name} cols={[
            { key: "n", head: "Scenario", cell: (x) => x.name },
            { key: "k", head: "Ok requests", align: "right", cell: (x) => x.s.latency.n_ok },
            { key: "f5", head: "First token p50", align: "right", cell: (x) => fmtMs(x.s.latency.ft.p50) },
            { key: "f9", head: "p95", align: "right", cell: (x) => fmtMs(x.s.latency.ft.p95) },
            { key: "f99", head: "p99", align: "right", title: "Only with at least 100 requests", cell: (x) => (x.s.latency.ft.p99 === null ? "n<100" : fmtMs(x.s.latency.ft.p99)) },
          ]} />}
        />
        <DataTable rows={standIn} rowKey={(x) => x.name} cols={[
          { key: "n", head: "Scenario", cell: (x) => <span className="flex items-center gap-2">{x.name}<SourceTag source="stand-in" /></span> },
          { key: "s", head: "Sent", align: "right", cell: (x) => x.s.n },
          { key: "ok", head: "Succeeded", align: "right", cell: (x) => fmtPct(x.s.by_status.ok ?? 0, x.s.n) },
          { key: "rl", head: "Rate-limited", align: "right", cell: (x) => fmtPct(x.s.by_status.rate_limited ?? 0, x.s.n) },
          { key: "to", head: "Timed out", align: "right", cell: (x) => fmtPct(x.s.by_status.timeout ?? 0, x.s.n) },
          { key: "re", head: "Retries per request", align: "right", cell: (x) => fmtNum(x.s.retries_per_request, 2) },
          { key: "tk", head: "Tokens per answer (simulated)", align: "right", cell: (x) => fmtNum(x.s.tokens_per_answer, 0) },
        ]} />
      </section>

      {data.sources && (
        <section>
          <h3 className="mb-1 text-[15px] font-semibold tracking-tight">Where the rows came from</h3>
          <p className="max-w-[78ch] text-[13px] text-ink2">The latest run of each scenario is merged into <code>results.csv</code>; every request id starts with its run id, and the run folders are never modified.</p>
          <details className="mt-2 text-[13px]">
            <summary className="cursor-pointer">Runs used ({data.sources.runs_used?.length ?? 0}) and runs left out ({data.sources.runs_left_out?.length ?? 0})</summary>
            <ul className="mt-2 space-y-0.5 tnum">
              {data.sources.runs_used?.map((r) => <li key={r.run_id}>{r.scenario}: {r.run_id} ({r.rows} rows{r.incomplete ? ", incomplete" : ""})</li>)}
              {data.sources.runs_left_out?.map((r) => <li key={r.run_id} className="text-ink2">Left out: {r.run_id}, {r.reason}</li>)}
            </ul>
          </details>
        </section>
      )}
    </div>
  );
}
