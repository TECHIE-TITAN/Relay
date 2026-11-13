"""V0 verification (DESIGN.md 3.2): mandatory, local, no provider quota, never shed.

Checks, per answer, against the sources actually sent for THIS request:
  1 citation validity     every cited label exists among the sources sent
  2 citation coverage     every factual sentence carries at least one citation
                          (refusal sentences and the extractive header are exempt)
  3 quote fidelity        text in double quotes occurs in a cited source (case, punctuation,
                          hyphenation ignored)
  4 number/entity grounding  numbers, acronyms and capitalised names in a sentence occur in its cited sources
  5 lexical support       enough of a sentence's content words occur in its cited sources
  6 refusal consistency   a refusal does not coexist with uncited claims

LIMITS, stated plainly: check 5 is word overlap, NOT entailment. A sentence built from the
source's own words can pass while meaning something the source does not say, and a correct
paraphrase with few shared words can be rejected. The threshold is uncalibrated. This tier
catches invented citations, fabricated quotes, wrong numbers and unrelated claims; it does not
prove an answer true.

`extractive_answer` builds an answer from verbatim source text so it passes by construction;
the pipeline still runs V0 on it.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from .schema import Chunk, Hit
from .textutil import normalize_for_match, phrase_in, tokenize

CITE_RE = re.compile(r"\[(S\d+(?:\s*,\s*S\d+)*)\]")
_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[\"(])|\n+")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_QUOTE_RE = re.compile(r"[\"“]([^\"“”]{4,})[\"”]")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,}\b")
_NAME_RE = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")     # Transformer, ImageNet, BLEU
REFUSAL_RE = re.compile(
    r"(the papers? (?:do|does) not (?:say|state|specify|contain|mention|cover|discuss)"
    r"|do(?:es)? not contain enough information"
    r"|not (?:stated|specified|mentioned|covered|discussed) in the (?:provided )?(?:papers?|sources?|excerpts?)"
    r"|cannot be answered from)", re.I,
)
EXTRACTIVE_PREFIX = "Closest passage in the papers (extractive, no model used):"
_EXEMPT_RE = re.compile(r"^(?:closest passages? in the papers|note:)", re.I)

SUPPORT_THRESHOLD = 0.5      # uncalibrated starting value


@dataclass
class V0Result:
    passed: bool
    refusal: bool
    failures: list = field(default_factory=list)       # distinct reason codes
    checks: dict = field(default_factory=dict)          # check name -> passed
    cited: list = field(default_factory=list)
    ms: float = 0.0
    details: list = field(default_factory=list)         # human-readable "code: offending text", for diagnosis and grading


def split_sentences(text: str) -> list:
    parts = [_BULLET_RE.sub("", p).strip() for p in _SPLIT_RE.split(text) if p and p.strip()]
    merged: list = []
    for p in parts:
        words = len(re.findall(r"[A-Za-z0-9]+", CITE_RE.sub("", p)))
        if merged and words < 3:
            merged[-1] += " " + p          # a stray citation or fragment belongs to the previous sentence
        else:
            merged.append(p)
    return merged


def cited_ids(sentence: str) -> list:
    out: list = []
    for m in CITE_RE.finditer(sentence):
        out.extend(x.strip() for x in m.group(1).split(","))
    return out


def verify_v0(answer: str, sources: Mapping[str, Chunk], *, support_threshold: float = SUPPORT_THRESHOLD,
              question: str = "") -> V0Result:
    """`question` (optional): names and acronyms the user already wrote are not 'ungrounded' when the
    answer repeats them (post-pilot change, DESIGN.md 16). Numbers and quotes are still checked
    against the evidence only."""
    t0 = time.perf_counter()
    q_words = set(tokenize(question, drop_stopwords=False)) if question else set()
    failures: set = set()
    checks = {k: True for k in ("citation_validity", "citation_coverage", "quote_fidelity",
                                "number_entity_grounding", "lexical_support", "refusal_consistency")}
    cited_all: list = []
    sentences = split_sentences(answer or "")
    if not sentences:
        return V0Result(False, False, ["empty_answer"], {k: False for k in checks}, [], (time.perf_counter() - t0) * 1000)

    details: list = []

    def bad(check: str, code: str, detail: str = "") -> None:
        checks[check] = False
        failures.add(code)
        if detail:
            details.append(f"{code}: {detail[:160]}")

    refusal_seen = False
    uncited_claim = False
    claim_sentences = 0
    for s in sentences:
        ids = cited_ids(s)
        cited_all.extend(ids)
        valid = [i for i in ids if i in sources]
        if any(i not in sources for i in ids):
            bad("citation_validity", "invalid_citation")
        is_refusal = bool(REFUSAL_RE.search(s))
        if is_refusal:
            refusal_seen = True
        if is_refusal or _EXEMPT_RE.match(s):
            if not _EXEMPT_RE.match(s):
                continue
        else:
            claim_sentences += 1
            if not valid:
                uncited_claim = True
                bad("citation_coverage", "uncited_claim", s)
                continue
        if not valid:
            continue
        raw = " ".join(sources[i].text for i in valid)
        norm = normalize_for_match(raw)
        body = CITE_RE.sub("", s).replace(EXTRACTIVE_PREFIX, " ").strip()
        for q in _QUOTE_RE.findall(body):
            if not phrase_in(norm, q):
                bad("quote_fidelity", "fabricated_quote", q)
        ev_nums = set(_NUM_RE.findall(raw))
        missing_nums = [n for n in _NUM_RE.findall(body) if n not in ev_nums]
        if missing_nums:
            bad("number_entity_grounding", "ungrounded_number", ", ".join(missing_nums))
        ev_words = set(norm.split())
        words = body.split()
        acr = [a.lower() for a in _ACRONYM_RE.findall(body)]
        names = [n.lower() for n in _NAME_RE.findall(" ".join(words[1:]))]
        missing_ents = [w for w in acr + names if w not in ev_words and w not in q_words]
        if missing_ents:
            bad("number_entity_grounding", "ungrounded_entity", ", ".join(missing_ents))
        toks = [t for t in tokenize(body) if not t.isdigit()]
        if len(toks) >= 4:
            ev_tokens = set(tokenize(raw))
            if sum(1 for t in toks if t in ev_tokens) / len(toks) < support_threshold:
                bad("lexical_support", "unsupported_sentence", s)
    if refusal_seen and uncited_claim:
        bad("refusal_consistency", "refusal_with_uncited_claims")
    refusal_only = refusal_seen and claim_sentences == 0
    return V0Result(
        passed=not failures, refusal=refusal_only, failures=sorted(failures), checks=checks,
        cited=sorted(set(cited_all)), ms=(time.perf_counter() - t0) * 1000.0, details=details,
    )


def _chunk_sentences(text: str) -> list:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text) if s.strip()]


# --- extractive relevance (post-pilot change, DESIGN.md 16) -----------------------------------------
MIN_EXTRACTIVE_OVERLAP = 2           # distinct content words a sentence must share with the question
SUBJECT_BONUS = 2                    # score bonus for a sentence that names the question's subject
_QUESTION_WORDS = frozenset({
    "what", "how", "which", "who", "whom", "whose", "when", "where", "why", "according", "does", "did", "do",
    "is", "are", "was", "were", "can", "could", "table", "figure", "section", "appendix", "the", "and", "for",
})
# "Nguyen et al., 2023", "Nguyen and Lee 2023", "(Smith, 2021)": an in-text citation naming another work.
_ATTRIB_RE = re.compile(r"\b([A-Z][A-Za-z\-]+)(?:\s+et\s+al\.?|\s+and\s+[A-Z][A-Za-z\-]+)?,?\s*\(?(?:19|20)\d{2}[a-z]?\b")
_ETAL_RE = re.compile(r"\bet\s+al\b", re.I)
# Title-page text (author lists, affiliations) is not an answer. PDF extraction often leaves it as one long
# "sentence" full of question words, so a sentence containing an e-mail address is never quoted.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def question_entities(question: str) -> set:
    """Lower-cased names and acronyms the user wrote (Moshi, GPT, H-Net's 'Net'), without question words."""
    found = _NAME_RE.findall(question) + _ACRONYM_RE.findall(question)
    return {w.lower() for w in found if w.lower() not in _QUESTION_WORDS}


def attributed_elsewhere(sentence: str, question: str, entities: set) -> bool:
    """True when the sentence clearly credits its claim to ANOTHER work: it carries an in-text citation, it does not
    mention the question's subject, and the question does not name the cited work. A sentence about the subject
    that merely cites support ('Moshi ... (Smith et al., 2023)') is kept, and so is every sentence when the
    question names no entity (there is then no subject to be absent)."""
    if not entities:
        return False
    marks = list(_ATTRIB_RE.finditer(sentence))
    if not marks and not _ETAL_RE.search(sentence):
        return False
    if entities & set(tokenize(sentence, drop_stopwords=False)):
        return False
    cited = {m.group(1).lower() for m in marks}
    return not (cited & set(tokenize(question, drop_stopwords=False)))


def extractive_answer(question: str, hits: Sequence[Hit], *, max_hits: int = 2) -> Optional[str]:
    """The best-matching verbatim sentence from the top hits, quoted and cited.

    A sentence qualifies only if it shares at least MIN_EXTRACTIVE_OVERLAP content words with the question, is
    not clearly about another work (`attributed_elsewhere`) and contains no e-mail address (title-page text).
    Sentences naming the question's subject get a bonus.
    None (the pipeline then refuses honestly) if nothing qualifies. This is a heuristic, not a relevance guarantee."""
    q = set(tokenize(question))
    ents = question_entities(question)
    best = (0, None, None)   # (score, sentence, label)
    for i, h in enumerate(hits[:max_hits], 1):
        for sent in _chunk_sentences(h.chunk.text):
            if len(sent.split()) < 6 or _EMAIL_RE.search(sent):
                continue
            overlap = len(q & set(tokenize(sent)))
            if overlap < MIN_EXTRACTIVE_OVERLAP or attributed_elsewhere(sent, question, ents):
                continue
            score = overlap + (SUBJECT_BONUS if ents & set(tokenize(sent, drop_stopwords=False)) else 0)
            if score > best[0]:
                best = (score, sent, f"S{i}")
    if best[1] is None:
        return None
    quote = best[1].replace('"', "'").strip().rstrip(".!?;:")
    return f'{EXTRACTIVE_PREFIX} "{quote}" [{best[2]}].'
