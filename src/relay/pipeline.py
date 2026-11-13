"""The request pipeline (DESIGN.md section 3).

  admit -> understand -> retrieve (TOOL) -> decide (BRANCH / LOOP) -> draft (LLM) -> verify V0 -> release

* Admission: bounded door queue plus an in-flight cap (admission.py). Everything below runs
  inside one slot, under ONE absolute deadline set at arrival and never extended.
* Understand: rule-based (paper names in the question). No LLM rewrite (scope cut).
* Retrieve: a local tool with its own timeout and optional failure injection (SIMULATED).
* Decide: BRANCH on evidence. No evidence -> refuse with no model call. Weak evidence ->
  optionally one reformulate-and-retrieve LOOP (off by default: it loosens the abstain rule
  that the retrieval study measured). Evidence -> draft.
* Degradation ladder from gateway pressure. Level 0/1: full path (there is no V1 in this build, so
  1 changes nothing). Level 2: fewer chunks, lower max tokens. Level 3: extractive answer, no model call.
  Level 4: cached answer or an honest "busy". V0 verification runs at EVERY level.
* Draft: one gateway call, buffered. Buffer, verify, release: nothing reaches the user before V0
  passes, so "first token" for the user is the release time; the model's own first token is
  logged separately (`model_first_token`).
* Verify (V0, mandatory): on failure regenerate once (a discretionary call, only when pressure
  is low and time allows), then fall back to a verbatim extractive answer, then refuse. An answer
  that failed V0 is never released.

Status mapping (the brief's enum has no "shed"): shed at the door or deadline passed -> timeout;
429s / no affordable quota -> rate_limited; tool, 5xx or network failures -> error. The finer
reason is in the `done` event (fail_stage, fail_reason, shed_reason).
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from .admission import Admission, Shed
from .config import PaperSpec
from .gateway import Gateway
from .prompts import NO_EVIDENCE_TEXT, build_messages, render_citations, source_map
from .retrieve import Retriever, detect_papers
from .schema import Hit, SearchResult
from .textutil import normalize_for_match, tokenize
from .tracing import EventLog, RequestContext
from .verify import V0Result, extractive_answer, verify_v0

BUSY_TEXT = "The service is at its limit right now. Please try again in a few minutes."
UNVERIFIED_TEXT = "I could not produce an answer that passed the checks, so I am not giving one."


@dataclass(frozen=True)
class PipelineConfig:
    setting: str = "S2"
    deadline_s: float = 20.0
    tool_timeout_s: float = 2.0
    tool_fail_p: float = 0.0            # SIMULATED failure injection into the retrieval tool
    tool_slow_p: float = 0.0
    tool_slow_s: float = 3.0
    max_tokens: int = 600
    use_retriever_abstain: bool = True
    reformulate_once: bool = False
    regenerate: bool = True
    ladder_enabled: bool = True
    ladder_l1: float = 0.70             # starting values, uncalibrated
    ladder_l2: float = 0.85
    ladder_l3: float = 0.95
    l2_k_factor: float = 0.5
    l2_max_tokens_factor: float = 0.6
    reserve_s: float = 0.3
    cache_enabled: bool = False
    support_threshold: float = 0.5
    seed: int = 1


@dataclass
class RequestOutcome:
    status: str = "error"
    answer_mode: str = "none"           # llm | extractive | refusal | busy | cache | none
    answer: str = ""
    first_token_ms: Optional[float] = None
    total_ms: float = 0.0
    fail_stage: Optional[str] = None
    fail_reason: Optional[str] = None
    shed_reason: Optional[str] = None
    ladder_level: int = 0
    regenerated: bool = False
    v0_passed: Optional[bool] = None
    v0_failures: list = field(default_factory=list)
    hits: list = field(default_factory=list)
    abstained: bool = False

    def fail(self, status: str, stage: str, reason: str) -> None:
        self.status = status
        self.fail_stage = stage
        self.fail_reason = reason


def reformulate(question: str, retriever: Retriever, min_idf: float = 1.5) -> Optional[str]:
    """Drop the question's common words (low idf in this corpus). None if nothing would change."""
    toks = list(dict.fromkeys(tokenize(question)))
    kept = [t for t in toks if retriever.index.idf(t) >= min_idf]
    if not kept or len(kept) == len(toks):
        return None
    return " ".join(kept)


class Pipeline:
    def __init__(self, retrievers: Mapping[str, Retriever], papers: Sequence[PaperSpec], gateway: Gateway,
                 admission: Admission, cfg: PipelineConfig, log: EventLog,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.retrievers = dict(retrievers)
        self.papers = list(papers)
        self.gateway = gateway
        self.admission = admission
        self.cfg = cfg
        self.log = log
        self.clock = clock
        self.cache: dict = {}

    # ------------------------------------------------------------------ entry point

    async def handle(self, ctx: RequestContext, question: str) -> RequestOutcome:
        out = RequestOutcome()
        ctx.stage = "arrive"
        ctx.emit("arrive", question_id=ctx.question_id, setting=self.cfg.setting, deadline_s=self.cfg.deadline_s)
        try:
            await asyncio.wait_for(self._body(ctx, question, out), timeout=max(0.001, ctx.remaining()))
        except asyncio.TimeoutError:
            out.fail("timeout", ctx.stage, "deadline_expired")
            out.answer_mode = "none"
            out.first_token_ms = None
        except Exception as e:     # a bug must not kill a load run: record it as an error, visibly
            out.fail("error", ctx.stage, f"internal:{type(e).__name__}")
            ctx.emit("internal_error", error=f"{type(e).__name__}: {e}"[:300])
        out.total_ms = (self.clock() - ctx.arrive) * 1000.0
        ctx.stage = "done"
        ctx.emit(
            "done", status=out.status, answer_mode=out.answer_mode, fail_stage=out.fail_stage,
            fail_reason=out.fail_reason, shed_reason=out.shed_reason, ladder_level=out.ladder_level,
            first_token_ms=None if out.first_token_ms is None else round(out.first_token_ms, 1),
            total_ms=round(out.total_ms, 1), retries=ctx.retries, attempts=ctx.attempts,
            discretionary_calls=ctx.discretionary_calls, tokens_in=ctx.tokens_in, tokens_out=ctx.tokens_out,
            tokens_unaccounted_est=ctx.tokens_unaccounted_est, regenerated=out.regenerated,
            v0_passed=out.v0_passed, v0_failures=out.v0_failures, abstained=out.abstained,
            retrieval_setting=self.cfg.setting, question_id=ctx.question_id,
        )
        return out

    # ------------------------------------------------------------------ stages

    def _cache_key(self, question: str) -> tuple:
        return (normalize_for_match(question), self.cfg.setting)

    async def _body(self, ctx: RequestContext, question: str, out: RequestOutcome) -> None:
        cfg = self.cfg
        if cfg.cache_enabled and self._cache_key(question) in self.cache:
            text, mode = self.cache[self._cache_key(question)]
            ctx.stage = "release"
            self._release(ctx, out, text, "cache", {}, v0=None)
            return
        ctx.stage = "admit"
        try:
            waited = await self.admission.enter(ctx)
        except Shed as s:
            out.fail("timeout", "admit", f"shed:{s.reason}")
            out.shed_reason = s.reason
            ctx.emit("shed", reason=s.reason, **self.admission.snapshot())
            return
        ctx.emit("slot_acquired", wait_ms=round(waited * 1000), **self.admission.snapshot())
        t_slot = self.clock()
        try:
            await self._in_slot(ctx, question, out)
        finally:
            self.admission.release(self.clock() - t_slot)
            ctx.emit("slot_released")

    def _level(self) -> tuple:
        p = self.gateway.pressure()
        if not self.cfg.ladder_enabled:
            return 0, p
        if p["exhausted"]:
            return 4, p
        queue_frac = self.admission.head_wait_s() / max(0.001, self.cfg.deadline_s)
        x = max(p["rpm"], p["tpm"], p["daily"], queue_frac)
        c = self.cfg
        level = 3 if x >= c.ladder_l3 else 2 if x >= c.ladder_l2 else 1 if x >= c.ladder_l1 else 0
        if p["paused"]:
            level = max(level, 2)
        return level, {**p, "queue": queue_frac}

    async def _in_slot(self, ctx: RequestContext, question: str, out: RequestOutcome) -> None:
        cfg = self.cfg
        ctx.stage = "understand"
        keys = detect_papers(question, self.papers) or None
        ctx.emit("understand", papers=keys or [])
        level, pressure = self._level()
        out.ladder_level = level
        ctx.emit("ladder", level=level, pressure={k: (round(v, 3) if isinstance(v, float) else v) for k, v in pressure.items()})
        # Level 4 (budget exhausted): retrieval is local and a refusal needs no model call, so both still
        # run. Only a request that would need the model is answered "busy" (below, at the draft stage).
        retriever = self.retrievers[cfg.setting]
        k = retriever.setting.k
        if level == 2:
            k = max(1, int(round(k * cfg.l2_k_factor)))
        res = await self._retrieve(ctx, out, retriever, question, keys, k)
        if res is None:
            return

        ctx.stage = "decide"
        if res.abstain and cfg.use_retriever_abstain:
            if cfg.reformulate_once and res.abstain_reason == "low_coverage" and ctx.remaining() > cfg.reserve_s + 1.0:
                q2 = reformulate(question, retriever)
                ctx.emit("loop_reformulate", before=question, after=q2, coverage_before=round(res.coverage, 3))
                if q2:
                    res2 = await self._retrieve(ctx, out, retriever, q2, keys, k)
                    if res2 is None:
                        return
                    ctx.stage = "decide"
                    res = res2
            if res.abstain:
                out.abstained = True
                ctx.emit("decide", branch="refuse_no_evidence", reason=res.abstain_reason, coverage=round(res.coverage, 3))
                self._release(ctx, out, NO_EVIDENCE_TEXT, "refusal", {}, v0=None)
                return
        hits = list(res.hits)
        if not hits:
            out.abstained = True
            ctx.emit("decide", branch="refuse_no_evidence", reason="no_hits")
            self._release(ctx, out, NO_EVIDENCE_TEXT, "refusal", {}, v0=None)
            return
        ctx.emit("decide", branch="draft", coverage=round(res.coverage, 3), n_hits=len(hits))
        sources = source_map(hits)
        out.hits = [h.chunk.chunk_id for h in hits]

        ctx.stage = "draft"
        if level >= 4:
            self._busy(ctx, out, "ladder_level_4")
            return
        if level >= 3:
            ctx.emit("degrade", to="extractive", level=level)
            self._extractive_or_refuse(ctx, out, question, hits, sources, "ladder_level_3")
            return
        max_tokens = cfg.max_tokens if level < 2 else max(64, int(cfg.max_tokens * cfg.l2_max_tokens_factor))
        outcome = await self.gateway.call(ctx, build_messages(question, hits), max_tokens=max_tokens, purpose="draft")
        if not outcome.ok:
            self._on_draft_failure(ctx, out, question, hits, sources, outcome)
            return
        assert outcome.result is not None
        ctx.emit("model_first_token", ttft_ms=outcome.result.ttft_ms)
        raw = outcome.result.text

        v0 = self._verify(ctx, out, raw, sources, attempt=1)
        if v0.passed:
            self._release(ctx, out, raw, "refusal" if v0.refusal else "llm", sources, v0=v0)
            return

        # V0 failed: regenerate once if pressure is low and time and attempts allow.
        if (cfg.regenerate and level <= 1 and ctx.attempts < self.gateway.cfg.max_attempts_per_request
                and ctx.remaining() >= self.gateway.call_estimate_s + cfg.reserve_s + 0.5):
            out.regenerated = True
            ctx.stage = "draft"
            ctx.emit("loop_regenerate", reasons=v0.failures)
            o2 = await self.gateway.call(ctx, build_messages(question, hits, strict_reasons=v0.failures),
                                         max_tokens=max_tokens, purpose="regenerate", discretionary=True)
            if o2.ok and o2.result is not None:
                v02 = self._verify(ctx, out, o2.result.text, sources, attempt=2)
                if v02.passed:
                    self._release(ctx, out, o2.result.text, "refusal" if v02.refusal else "llm", sources, v0=v02)
                    return
        ctx.emit("degrade", to="extractive", reason="v0_failed")
        self._extractive_or_refuse(ctx, out, question, hits, sources, "v0_failed")

    async def _retrieve(self, ctx: RequestContext, out: RequestOutcome, retriever: Retriever, query: str,
                        keys: Optional[Sequence[str]], k: int) -> Optional[SearchResult]:
        cfg = self.cfg
        ctx.stage = "retrieve"
        n_calls = ctx.notes.get("tool_calls", 0)
        ctx.notes["tool_calls"] = n_calls + 1
        rng = random.Random(f"{cfg.seed}:{ctx.request_id}:{n_calls}")
        for attempt in (1, 2):
            budget = min(cfg.tool_timeout_s, ctx.remaining() - cfg.reserve_s)
            if budget <= 0:
                out.fail("timeout", "retrieve", "no_time_for_tool")
                return None
            slow = rng.random() < cfg.tool_slow_p
            broken = rng.random() < cfg.tool_fail_p
            ctx.emit("tool_start", tool="retrieve", attempt=attempt, setting=cfg.setting, query=query, timeout_s=round(budget, 3))
            t0 = self.clock()

            async def tool() -> SearchResult:
                if slow:
                    await asyncio.sleep(cfg.tool_slow_s)
                if broken:
                    raise RuntimeError("injected retrieval failure")
                return retriever.search(query, paper_keys=keys, k=k)

            try:
                res = await asyncio.wait_for(tool(), timeout=budget)
            except asyncio.TimeoutError:
                err = "tool_timeout"
            except Exception as e:     # injected or real failure of the local tool
                err = f"tool_error:{type(e).__name__}"
            else:
                ctx.emit("tool_end", tool="retrieve", ok=True, duration_ms=round((self.clock() - t0) * 1000, 2))
                ctx.emit("retrieval", setting=cfg.setting, hits=[h.chunk.chunk_id for h in res.hits],
                         scores=[round(h.score, 3) for h in res.hits], abstain=res.abstain,
                         abstain_reason=res.abstain_reason, coverage=round(res.coverage, 3))
                return res
            ctx.emit("tool_end", tool="retrieve", ok=False, error=err, duration_ms=round((self.clock() - t0) * 1000, 2))
            if attempt == 2:
                out.fail("error" if err.startswith("tool_error") else "timeout", "retrieve", err)
                return None
        return None

    def _verify(self, ctx: RequestContext, out: RequestOutcome, text: str, sources: Mapping[str, Any], attempt: int) -> V0Result:
        ctx.stage = "verify"
        v0 = verify_v0(text, sources, support_threshold=self.cfg.support_threshold)
        out.v0_passed = v0.passed
        out.v0_failures = list(v0.failures)
        ctx.emit("verify_result", attempt=attempt, passed=v0.passed, failures=v0.failures, checks=v0.checks,
                 refusal=v0.refusal, ms=round(v0.ms, 3), cited=v0.cited)
        return v0

    def _on_draft_failure(self, ctx: RequestContext, out: RequestOutcome, question: str, hits: Sequence[Hit],
                          sources: Mapping[str, Any], outcome: Any) -> None:
        f = outcome.failure
        rate_limited_last = (outcome.last_error_kind or "").startswith("rate_limit")
        if f == "daily_exhausted":
            self._busy(ctx, out, f)
        elif f in ("limiter_timeout", "budget_denied", "too_large") or (
                f in ("retries_exhausted", "attempt_cap") and rate_limited_last):
            ctx.emit("degrade", to="extractive", reason=f)
            self._extractive_or_refuse(ctx, out, question, hits, sources, f)
        else:
            out.fail(outcome.status, "draft", f or "unknown")

    def _extractive_or_refuse(self, ctx: RequestContext, out: RequestOutcome, question: str, hits: Sequence[Hit],
                              sources: Mapping[str, Any], reason: str) -> None:
        if ctx.remaining() <= self.cfg.reserve_s / 2:
            out.fail("timeout", ctx.stage, "no_time_for_fallback")
            return
        text = extractive_answer(question, hits)
        if text:
            v0 = self._verify(ctx, out, text, sources, attempt=3)
            if v0.passed:
                self._release(ctx, out, text, "extractive", sources, v0=v0)
                return
        out.fail_reason = f"unverified:{reason}"
        out.fail_stage = ctx.stage
        self._release(ctx, out, UNVERIFIED_TEXT, "refusal", {}, v0=None)

    def _busy(self, ctx: RequestContext, out: RequestOutcome, reason: str) -> None:
        out.fail("rate_limited", ctx.stage, reason)
        out.answer_mode = "busy"
        out.answer = BUSY_TEXT
        ctx.emit("busy", reason=reason)

    def _release(self, ctx: RequestContext, out: RequestOutcome, text: str, mode: str,
                 sources: Mapping[str, Any], v0: Optional[V0Result]) -> None:
        ctx.stage = "release"
        if ctx.remaining() <= 0:
            out.fail("timeout", "release", "deadline_at_release")
            ctx.emit("not_released", reason="deadline_passed")
            return
        out.answer = render_citations(text, sources)
        out.answer_mode = mode
        out.status = "ok"
        now = self.clock()
        ctx.release_at = now
        out.first_token_ms = (now - ctx.arrive) * 1000.0
        ctx.emit("first_token", user_visible=True, ms=round(out.first_token_ms, 1), mode=mode)
        ctx.emit("last_token")
        if self.cfg.cache_enabled and mode == "llm" and v0 is not None and v0.passed:
            self.cache[self._cache_key(ctx.question)] = (out.answer, mode)   # the rendered answer
