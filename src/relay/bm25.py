"""Okapi BM25, written out in full so every number can be checked by hand.

score(d, q) = sum over distinct query terms t of
    idf(t) * tf(t, d) * (k1 + 1) / (tf(t, d) + k1 * (1 - b + b * len(d) / avgdl))
    idf(t)  = ln(1 + (N - df + 0.5) / (df + 0.5))        (always positive)

Defaults k1 = 1.5 and b = 0.75 are conventional starting values, not tuned.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Sequence


class BM25Index:
    def __init__(
        self,
        postings: dict[str, list[tuple[int, int]]],
        doc_len: list[int],
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self.postings = postings
        self.doc_len = doc_len
        self.n = len(doc_len)
        self.k1 = k1
        self.b = b
        self.avgdl = (sum(doc_len) / self.n) if self.n else 0.0

    @classmethod
    def build(cls, docs_tokens: Sequence[Sequence[str]], k1: float = 1.5, b: float = 0.75) -> "BM25Index":
        postings: dict[str, list[tuple[int, int]]] = {}
        doc_len: list[int] = []
        for doc_id, tokens in enumerate(docs_tokens):
            doc_len.append(len(tokens))
            for term, tf in Counter(tokens).items():
                postings.setdefault(term, []).append((doc_id, tf))
        return cls(postings, doc_len, k1, b)

    def idf(self, term: str) -> float:
        df = len(self.postings.get(term, ()))
        if df == 0:
            return 0.0
        return math.log(1.0 + (self.n - df + 0.5) / (df + 0.5))

    def score_all(self, query_tokens: Sequence[str]) -> list[float]:
        """Score every document (0.0 for documents sharing no query term)."""
        scores = [0.0] * self.n
        for term in set(query_tokens):
            plist = self.postings.get(term)
            if not plist:
                continue
            idf = self.idf(term)
            for doc_id, tf in plist:
                dl = self.doc_len[doc_id]
                denom = tf + self.k1 * (1.0 - self.b + self.b * dl / self.avgdl)
                scores[doc_id] += idf * tf * (self.k1 + 1.0) / denom
        return scores

    def search(self, query_tokens: Sequence[str], top_n: int = 10) -> list[tuple[int, float]]:
        """Top documents with positive score, ordered by (-score, doc_id)."""
        scores = self.score_all(query_tokens)
        ranked = sorted(
            ((i, s) for i, s in enumerate(scores) if s > 0.0), key=lambda x: (-x[1], x[0])
        )
        return ranked[:top_n]
