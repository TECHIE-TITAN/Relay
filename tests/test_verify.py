from __future__ import annotations

from relay.prompts import build_messages, cite_label, render_citations, source_map
from relay.schema import Chunk, Hit
from relay.verify import (
    EXTRACTIVE_PREFIX, attributed_elsewhere, extractive_answer, question_entities, split_sentences, verify_v0,
)


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


def test_failures_come_with_the_offending_text_for_diagnosis():
    r = verify_v0("The encoder has 7 identical layers [S1].", SOURCES)
    assert "ungrounded_number" in r.failures and any("7" in d and d.startswith("ungrounded_number") for d in r.details)
    r = verify_v0("The encoder has identical layers.", SOURCES)
    assert any(d.startswith("uncited_claim") and "identical layers" in d for d in r.details)
    assert verify_v0("The encoder is a stack of N = 6 identical layers [S1].", SOURCES).details == []


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


# ---- post-pilot regression tests (DESIGN.md 16). Texts are the real ones from the pilot logs. ----------------

Q01 = "How many identical layers are used in the Transformer's encoder stack?"
Q01_DRAFT = "The Transformer’s encoder stack uses six identical layers. [S1]"
Q01_CHUNK = chunk("a-6", "3.1 Encoder and Decoder Stacks Encoder: The encoder is composed of a stack of N = 6 identical "
                         "layers. Each layer has two sub-layers.")

Q08 = "How does Moshi represent the user's speech and its own generated speech during full-duplex dialogue?"
DGSLM = ("The only previous full-duplex dialogue system is dGSLM (Nguyen et al., 2023), which models user and "
         "system speech as separate audio token streams and proposes a Siamese architecture to process both streams jointly.")
GOLD = "Moshi casts dialogue as speech-to-speech generation, while modeling separately its own speech and that of the user into parallel streams."
Q08_RELATED = chunk("m-13", "Moshi is new. " + DGSLM, paper="moshi", page=6, section="2 Related Work")
Q08_ABSTRACT = chunk("m-0", GOLD, paper="moshi", page=1, section="Abstract")


def test_q01_a_name_from_the_question_is_not_ungrounded_but_a_new_name_still_is():
    src = {"S1": Q01_CHUNK}                                       # the chunk never says "Transformer"
    before = verify_v0(Q01_DRAFT, src)                            # no question given: the old behaviour
    assert not before.passed and before.failures == ["ungrounded_entity"] and before.details == ["ungrounded_entity: transformer"]
    after = verify_v0(Q01_DRAFT, src, question=Q01)
    assert after.passed and after.failures == [] and after.details == []
    invented = verify_v0("BERT’s encoder stack uses six identical layers. [S1]", src, question=Q01)
    assert not invented.passed and "ungrounded_entity" in invented.failures
    assert any(d.startswith("ungrounded_entity") and "bert" in d for d in invented.details)


def test_question_names_do_not_excuse_numbers_or_fabricated_quotes():
    src = {"S1": Q01_CHUNK}
    assert "ungrounded_number" in verify_v0("The Transformer’s encoder stack uses 12 identical layers [S1].", src, question=Q01).failures
    assert "fabricated_quote" in verify_v0('The Transformer’s encoder has "seven odd layers" [S1].', src, question=Q01).failures


def test_q08_extractive_fallback_skips_the_sentence_about_another_system():
    hits = [Hit(chunk=Q08_RELATED, score=3.0, rank=1), Hit(chunk=Q08_ABSTRACT, score=2.0, rank=2)]
    ans = extractive_answer(Q08, hits)
    assert ans is not None and "parallel streams" in ans and "dGSLM" not in ans and ans.endswith("[S2].")
    assert verify_v0(ans, {"S1": Q08_RELATED, "S2": Q08_ABSTRACT}, question=Q08).passed


def test_q08_with_only_the_prior_work_sentence_there_is_no_extractive_answer():
    assert extractive_answer(Q08, [Hit(chunk=Q08_RELATED, score=3.0, rank=1)]) is None


def test_citation_exclusion_is_not_blind_to_et_al():
    # a sentence about the question's own subject that merely cites support is kept
    own = chunk("m-1", "Moshi models user and system speech as parallel streams, following earlier work (Nguyen et al., 2023).",
                paper="moshi")
    assert extractive_answer(Q08, [Hit(chunk=own, score=1.0, rank=1)]) is not None
    # a question that itself names the cited author keeps that author's sentence
    ask = "What did Nguyen propose for full-duplex dialogue and user speech streams?"
    assert extractive_answer(ask, [Hit(chunk=Q08_RELATED, score=1.0, rank=1)]) is not None
    # a question naming no entity has no subject to be absent, so nothing is excluded on attribution alone
    plain = "which system models user speech as separate audio token streams?"
    assert extractive_answer(plain, [Hit(chunk=Q08_RELATED, score=1.0, rank=1)]) is not None
    # the unit helper agrees
    ents = question_entities(Q08)
    assert ents == {"moshi"} and attributed_elsewhere(DGSLM, Q08, ents) and not attributed_elsewhere(GOLD, Q08, ents)


TITLE_BLOB = ("Moshi: a speech-text foundation model for real-time dialogue Alexandre D ́efossez∗ alex@kyutai.org "
              "Laurent Mazar ́e∗ Manu Orsini Am ́elie Royer Patrick P ́erez Herv ́e J ́egou Edouard Grave∗ "
              "Neil Zeghidour∗ neil@kyutai.org Kyutai ∗Equal contribution Abstract We introduce Moshi, a speech-text "
              "foundation model and full-duplex spoken dialogue framework.")
BODY_SENTENCE = ("Starting from a text language model backbone, Moshi generates speech as tokens from the residual "
                 "quantizer of a neural audio codec, while modeling separately its own speech and that of the user "
                 "into parallel streams.")


def test_q08_title_page_blob_is_skipped_and_the_body_passage_is_preferred():
    # the pilot's third run quoted the title-page blob (author names and e-mails) for this question
    abstract = chunk("m-0", TITLE_BLOB + " Moshi solves these independent issues altogether by casting spoken "
                     "dialogue as speech-to-speech generation. " + BODY_SENTENCE, paper="moshi", page=1, section="Abstract")
    ans = extractive_answer(Q08, [Hit(chunk=abstract, score=2.0, rank=1)])
    assert ans is not None and "parallel streams" in ans
    assert "kyutai" not in ans and "@" not in ans and "Zeghidour" not in ans
    assert verify_v0(ans, {"S1": abstract}, question=Q08).passed


def test_when_only_title_page_text_remains_there_is_no_extractive_answer():
    only_blob = chunk("m-0", TITLE_BLOB + " Own user speech.", paper="moshi", page=1, section="Abstract")
    assert extractive_answer(Q08, [Hit(chunk=only_blob, score=2.0, rank=1)]) is None


def test_no_relevant_passage_means_none_even_with_a_single_shared_word():
    one_word = chunk("x-1", "The model runs on GPUs with hardware acceleration enabled today.")
    assert extractive_answer("What is the exact inference latency of GPT-4 on the same hardware as Moshi?",
                             [Hit(chunk=one_word, score=1.0, rank=1), Hit(chunk=C2, score=0.5, rank=2)]) is None


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
