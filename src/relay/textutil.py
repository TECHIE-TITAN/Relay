"""Text helpers: approximate token counts, tokenisation for BM25, matching.

`approx_tokens` is characters / 4. It is used only to size chunks and to
estimate context size offline. Real token counts always come from the
provider's `usage` field and are never replaced by this approximation.
"""
from __future__ import annotations

import math
import re
import unicodedata

# Decimals stay one token ("28.4"); everything else splits on non-alphanumerics,
# so "multi-head" becomes "multi", "head".
_TOKEN_RE = re.compile(r"\d+(?:\.\d+)+|[a-z0-9]+")

# Small on purpose. Negations ("not") are kept.
STOPWORDS = frozenset(
    """a an the of in on to and or is are was were be been by for with as at from
    that this these those it its which what how do does did we our can than then
    into over under about""".split()
)


def approx_tokens(text: str) -> int:
    """Approximate token count: ceil(characters / 4)."""
    return math.ceil(len(text) / 4) if text else 0


def word_cost(word: str) -> float:
    """Approximate token cost of one word plus its following space."""
    return (len(word) + 1) / 4


def tokenize(text: str, *, drop_stopwords: bool = True) -> list[str]:
    """Lower-cased tokens for BM25 (indexing and querying use the same function)."""
    tokens = _TOKEN_RE.findall(unicodedata.normalize("NFKC", text).lower())
    if drop_stopwords:
        tokens = [t for t in tokens if t not in STOPWORDS]
    return tokens


def normalize_for_match(text: str) -> str:
    """Lower-case, NFKC, non-alphanumerics collapsed to single spaces.

    Used to test whether an evidence phrase occurs in a chunk regardless of
    punctuation, hyphenation style and case.
    """
    t = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()
