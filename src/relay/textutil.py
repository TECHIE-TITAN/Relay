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
# Hyphenated compounds such as "position-wise" also yield "positionwise".
_COMPOUND_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+")

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
    lowered = unicodedata.normalize("NFKC", text).lower().replace("‐", "-").replace("‑", "-")
    tokens = _TOKEN_RE.findall(lowered)
    # Extraction re-joins words split at a line break ("position-" + "wise" -> "positionwise"),
    # so a hyphenated compound also emits its joined form. A query "position-wise" then matches
    # text containing either "position-wise" or "positionwise".
    tokens.extend(m.replace("-", "") for m in _COMPOUND_RE.findall(lowered))
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


def phrase_in(haystack_norm: str, phrase: str) -> bool:
    """Does `phrase` occur in `haystack_norm` (output of normalize_for_match)?

    Whole-word match first. For long phrases (12+ characters once spaces are
    removed) it falls back to a spacing-insensitive match, which tolerates
    words that extraction re-joined or split at a line break
    ("position-wise" vs "positionwise"). Short phrases never use the fallback,
    since squashing them could match across unrelated words.
    """
    needle = normalize_for_match(phrase)
    if not needle:
        return True
    if f" {needle} " in f" {haystack_norm} ":
        return True
    squashed = needle.replace(" ", "")
    return len(squashed) >= 12 and squashed in haystack_norm.replace(" ", "")
