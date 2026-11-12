"""Retrieval over the chunk files: BM25, optional MMR dedup, abstain signal.

`Retriever.search` is the function the pipeline's retrieval tool will wrap in
a later phase (timeouts and failure injection live there, not here).

Abstain: the best hit must contain at least `abstain_min_coverage` of the
distinct query terms. This is a scale-free relevance check (BM25 scores are
not comparable across queries). The threshold is a starting value to be
calibrated with `eval-retrieval`; it is not a measured result.

MMR ("rerank: mmr"): from `fetch_k` candidates pick `k`, trading relevance
against similarity (Jaccard of token sets) to chunks already chosen. It helps
when abstract, introduction and conclusion repeat the same claim, but it can
drop a genuinely needed neighbouring chunk, because overlapping chunks look
very similar.
"""
from __future__ import annotations

import time
from typing import Optional, Sequence

from .bm25 import BM25Index
from .chunk import read_chunks
from .config import Config, PaperSpec, RetrievalSetting
from .schema import Chunk, Hit, RelayError, SearchResult
from .textutil import tokenize


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


class Retriever:
    def __init__(
        self,
        setting: RetrievalSetting,
        chunks: Sequence[Chunk],
        *,
        abstain_min_coverage: float,
        mmr_lambda: float,
    ) -> None:
        self.setting = setting
        self.chunks = list(chunks)
        self.abstain_min_coverage = abstain_min_coverage
        self.mmr_lambda = mmr_lambda
        token_lists = [tokenize(c.text) for c in self.chunks]
        self._token_sets = [set(t) for t in token_lists]
        self.index = BM25Index.build(token_lists)

    @classmethod
    def from_config(cls, cfg: Config, setting_id: Optional[str] = None) -> "Retriever":
        setting = cfg.setting(setting_id or cfg.default_setting)
        chunks = read_chunks(cfg.paths.chunks_file(setting.id))
        if not chunks:
            raise RelayError(f"{cfg.paths.chunks_file(setting.id)} contains no chunks")
        return cls(
            setting,
            chunks,
            abstain_min_coverage=cfg.abstain_min_coverage,
            mmr_lambda=cfg.mmr_lambda,
        )

    def _mmr(self, cand: list[tuple[int, float]], k: int) -> list[tuple[int, float]]:
        """cand is sorted by descending score. Returns up to k selected items."""
        if not cand:
            return []
        max_score = cand[0][1]
        selected = [cand[0]]
        rest = list(cand[1:])
        lam = self.mmr_lambda
        while rest and len(selected) < k:
            best_idx, best_val = 0, float("-inf")
            for idx, (i, s) in enumerate(rest):
                rel = s / max_score if max_score > 0 else 0.0
                sim = max(_jaccard(self._token_sets[i], self._token_sets[j]) for j, _ in selected)
                val = lam * rel - (1.0 - lam) * sim
                if val > best_val:   # strict: ties keep the higher-scored candidate
                    best_idx, best_val = idx, val
            selected.append(rest.pop(best_idx))
        return selected

    def search(
        self,
        query: str,
        *,
        paper_keys: Optional[Sequence[str]] = None,
        k: Optional[int] = None,
    ) -> SearchResult:
        t0 = time.perf_counter()
        qtokens = tokenize(query)

        def done(hits: tuple[Hit, ...], abstain: bool, reason: Optional[str], coverage: float) -> SearchResult:
            return SearchResult(
                query=query,
                query_tokens=tuple(qtokens),
                hits=hits,
                abstain=abstain,
                abstain_reason=reason,
                coverage=coverage,
                elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            )

        if not qtokens:
            return done((), True, "empty_query", 0.0)

        scores = self.index.score_all(qtokens)
        allowed = set(paper_keys) if paper_keys else None
        cand = [
            (i, s)
            for i, s in enumerate(scores)
            if s > 0.0 and (allowed is None or self.chunks[i].paper_key in allowed)
        ]
        cand.sort(key=lambda x: (-x[1], self.chunks[x[0]].chunk_id))
        cand = cand[: self.setting.fetch_k]
        if not cand:
            return done((), True, "no_match", 0.0)

        uq = set(qtokens)
        coverage = len(uq & self._token_sets[cand[0][0]]) / len(uq)

        want = k or self.setting.k
        chosen = self._mmr(cand, want) if self.setting.rerank == "mmr" else cand[:want]
        hits = tuple(
            Hit(chunk=self.chunks[i], score=s, rank=r) for r, (i, s) in enumerate(chosen, 1)
        )
        low = coverage < self.abstain_min_coverage
        return done(hits, low, "low_coverage" if low else None, coverage)


def detect_papers(query: str, papers: Sequence[PaperSpec]) -> list[str]:
    """Paper keys the query names explicitly (by title or short name).

    Conservative on purpose: a bare word such as "attention" is NOT treated as
    naming the Attention paper, because it appears in all three papers.
    """
    q = query.lower()
    found: list[str] = []
    for p in papers:
        title = p.title.lower()
        aliases = {title, title.split(":")[0].strip()}
        spaced = p.key.replace("-", " ")
        if " " in spaced:
            aliases.add(spaced)
        if any(a and a in q for a in aliases):
            found.append(p.key)
    return found
