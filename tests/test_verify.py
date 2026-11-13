from __future__ import annotations

from relay.prompts import build_messages, cite_label, render_citations, source_map
from relay.schema import Chunk, Hit
from relay.verify import EXTRACTIVE_PREFIX, extractive_answer, split_sentences, verify_v0


def chunk(cid, text, paper="attention", page=3, section="3.1 Encoder"):
    return Chunk(chunk_id=cid, paper_key=paper, setting_id="S2", page_start=page, page_end=page,
                 section=section, text=text, approx_tokens=len(text) // 4, printed_start="2", printed_end="2")


C1 = chunk("a-1", "The encoder is composed of a stack of N = 6 identical layers. Each layer has two sub-layers. "
                  "The base model reached 27.3 BLEU on the WMT 2014 English-to-German task.")
C2 = chunk("m-1", "Mimi is a neural audio codec that uses residual vector quantization.", paper="moshi", page=9)
SOURCES = {"S1": C1, "S2": C2}


def test_grounded_cited_answer_passes():
    r = verify_v0("The encoder is a stack of N = 6 identical layers [S1].", SOURCES)
    assert r.passed and not r.refusal and r.failures == [] and r.cited == ["S1"]


def test_invented_citation_fails():
    r = verify_v0("The encoder is a stack of identical layers [S7].", SOURCES)
    assert not r.passed and "invalid_citation" in r.failures and not r.checks["citation_validity"]


def test_uncited_claim_fails():
    r = verify_v0("The encoder is a stack of identical layers.", SOURCES)
    assert "uncited_claim" in r.failures and not r.checks["citation_coverage"]


def test_second_sentence_uncited_fails_even_if_first_is_cited():
    r = verify_v0("The encoder has identical layers [S1]. Mimi is a codec.", SOURCES)
    assert "uncited_claim" in r.failures


def test_fabricated_quote_fails_and_verbatim_quote_passes():
    bad = verify_v0('The paper says "a stack of seven layers" [S1].', SOURCES)
    assert "fabricated_quote" in bad.failures
    good = verify_v0('The paper says "a stack of N = 6 identical layers" [S1].', SOURCES)
    assert good.passed


def test_wrong_number_fails():
    r = verify_v0("The base model reached 28.4 BLEU on the WMT 2014 English-to-German task [S1].", SOURCES)
    assert "ungrounded_number" in r.failures and not r.checks["number_entity_grounding"]


def test_unknown_acronym_or_name_fails():
    r = verify_v0("The base model reached 27.3 BLEU on the ImageNet 2014 English-to-German task [S1].", SOURCES)
    assert "ungrounded_entity" in r.failures
    r2 = verify_v0("The base model reached 27.3 ROUGE on the WMT 2014 task [S1].", SOURCES)
    assert "ungrounded_entity" in r2.failures


def test_unrelated_sentence_fails_lexical_support():
    r = verify_v0("Residual connections accelerate gradient propagation through deep architectures [S1].", SOURCES)
    assert "unsupported_sentence" in r.failures and not r.checks["lexical_support"]


def test_refusal_alone_passes_and_is_marked_refusal():
    r = verify_v0("The papers do not say.", SOURCES)
    assert r.passed and r.refusal


def test_refusal_mixed_with_uncited_claim_fails():
    r = verify_v0("The papers do not say. The model has 12 layers.", SOURCES)
    assert not r.passed and "refusal_with_uncited_claims" in r.failures and not r.checks["refusal_consistency"]


def test_refusal_with_a_cited_partial_answer_passes():
    r = verify_v0("The papers do not say the dataset size. The encoder has six identical layers [S1].", SOURCES)
    assert r.passed and not r.refusal


def test_empty_answer_fails():
    assert verify_v0("   ", SOURCES).failures == ["empty_answer"]


def test_citation_after_period_attaches_to_previous_sentence():
    assert split_sentences("The encoder has six layers. [S1]") == ["The encoder has six layers. [S1]"]
    r = verify_v0("The encoder has six identical layers. [S1]", SOURCES)
    assert r.passed


def test_multi_citation_and_list_markers():
    r = verify_v0("- The encoder has six identical layers [S1].\n- Mimi is a neural audio codec [S2].", SOURCES)
    assert r.passed and r.cited == ["S1", "S2"]
    r2 = verify_v0("The encoder has six identical layers and Mimi is a neural audio codec [S1, S2].", SOURCES)
    assert r2.passed


def test_extractive_answer_passes_v0_by_construction():
    hits = [Hit(chunk=C1, score=2.0, rank=1), Hit(chunk=C2, score=1.0, rank=2)]
    ans = extractive_answer("How many identical layers are in the encoder?", hits)
    assert ans is not None and ans.startswith(EXTRACTIVE_PREFIX) and ans.endswith("[S1].")
    r = verify_v0(ans, SOURCES)
    assert r.passed, r.failures


def test_extractive_answer_returns_none_without_overlap():
    hits = [Hit(chunk=C2, score=1.0, rank=1)]
    assert extractive_answer("zebra quantum helicopter", hits) is None


def test_prompt_labels_sources_and_render_citations_is_readable():
    hits = [Hit(chunk=C1, score=2.0, rank=1), Hit(chunk=C2, score=1.0, rank=2)]
    msgs = build_messages("How many layers?", hits)
    user = msgs[1]["content"]
    assert user.startswith("Sources:\n[S1] attention | pdf p.3 | 3.1 Encoder\n")
    assert "[S2] moshi | pdf p.9" in user and user.endswith("Question: How many layers?")
    strict = build_messages("q", hits, strict_reasons=["uncited_claim"])
    assert "uncited_claim" in strict[0]["content"] and "uncited_claim" not in msgs[0]["content"]
    out = render_citations("Six layers [S1]. Codec [S1, S2]. Odd [S9].", source_map(hits))
    assert "[attention, pdf p.3 (printed 2), §3.1 Encoder]" in out
    assert "[attention, pdf p.3 (printed 2), §3.1 Encoder; moshi, pdf p.9 (printed 2), §3.1 Encoder]" in out
    assert "[S9]" in out
    assert cite_label(C2).startswith("moshi, pdf p.9")
