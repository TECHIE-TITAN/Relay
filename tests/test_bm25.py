import math

import pytest

from relay.bm25 import BM25Index
from relay.textutil import approx_tokens, normalize_for_match, phrase_in, tokenize

# Hand-worked example. Docs: d0 = [a, b] (len 2), d1 = [a, a, c] (len 3).
# N = 2, avgdl = 2.5, k1 = 1.5, b = 0.75.
DOCS = [["a", "b"], ["a", "a", "c"]]


def test_single_term_score_matches_hand_calculation():
    idx = BM25Index.build(DOCS)
    # term "b": df = 1 -> idf = ln(1 + (2 - 1 + 0.5) / (1 + 0.5)) = ln 2
    # d0: tf = 1, dl = 2 -> denom = 1 + 1.5 * (1 - 0.75 + 0.75 * 2 / 2.5) = 2.275; numerator = 1 * 2.5
    scores = idx.score_all(["b"])
    assert scores[0] == pytest.approx(math.log(2) * 2.5 / 2.275, rel=1e-9)
    assert scores[1] == 0.0


def test_term_frequency_and_length_normalisation_rank_correctly():
    idx = BM25Index.build(DOCS)
    # term "a": df = 2 -> idf = ln(1 + 0.5 / 2.5) = ln 1.2
    # d0: 2.5 / 2.275 ; d1: tf = 2, dl = 3 -> denom = 2 + 1.5 * (0.25 + 0.75 * 3 / 2.5) = 3.725, numerator = 5
    scores = idx.score_all(["a"])
    assert scores[0] == pytest.approx(math.log(1.2) * 2.5 / 2.275, rel=1e-9)
    assert scores[1] == pytest.approx(math.log(1.2) * 5.0 / 3.725, rel=1e-9)
    assert [i for i, _ in idx.search(["a"])] == [1, 0]


def test_unknown_and_empty_queries_score_zero():
    idx = BM25Index.build(DOCS)
    assert idx.score_all(["zzz"]) == [0.0, 0.0]
    assert idx.score_all([]) == [0.0, 0.0]
    assert idx.search(["zzz"]) == []


def test_duplicate_query_terms_count_once():
    idx = BM25Index.build(DOCS)
    assert idx.score_all(["a", "a", "a"]) == idx.score_all(["a"])


def test_known_item_search_returns_the_right_document_first():
    docs = [tokenize(t) for t in [
        "multi head attention runs several attention functions in parallel",
        "the codec encodes speech into discrete audio tokens",
        "dynamic chunking learns segment boundaries end to end",
    ]]
    idx = BM25Index.build(docs)
    assert idx.search(tokenize("audio codec tokens"))[0][0] == 1
    assert idx.search(tokenize("segment boundaries"))[0][0] == 2


def test_tokenize_keeps_decimals_splits_hyphens_and_drops_stopwords():
    assert tokenize("28.4 BLEU") == ["28.4", "bleu"]
    assert tokenize("multi-head attention") == ["multi", "head", "attention", "multihead"]
    assert tokenize("What is the base model") == ["base", "model"]
    assert tokenize("not") == ["not"]            # negation is kept


def test_hyphenated_and_rejoined_words_share_a_token():
    # extraction re-joins "position-" + "wise" into "positionwise"; the query keeps the hyphen
    assert "positionwise" in tokenize("position-wise feed-forward")
    assert set(tokenize("position-wise")) & set(tokenize("a positionwise network"))
    assert tokenize("4.5-5 bytes") == ["4.5", "5", "bytes"]        # numbers are not compounds
    assert "hnet" in tokenize("H-Net")


def test_phrase_in_tolerates_rejoined_hyphenation_for_long_phrases_only():
    hay = normalize_for_match("a simple, positionwise fully connected feed-forward network")
    assert phrase_in(hay, "position-wise fully connected feed-forward network")
    assert not phrase_in(hay, "fully connected recurrent network")
    assert phrase_in(normalize_for_match("O(1) steps"), "O(1)")
    assert not phrase_in(normalize_for_match("go1 ahead"), "o 1")   # short phrase: no squashed fallback


def test_text_helpers():
    assert approx_tokens("") == 0
    assert approx_tokens("abcd") == 1
    assert approx_tokens("abcde") == 2
    assert normalize_for_match("Scaled Dot-Product  Attention!") == "scaled dot product attention"
