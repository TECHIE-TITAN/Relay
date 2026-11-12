import pytest

from relay.config import RetrievalSetting
from relay.eval_retrieval import (
    Gold, QuestionsError, Question, chunk_satisfies, evaluate, format_table, load_questions,
)
from relay.retrieve import Retriever
from relay.schema import Chunk
from relay.textutil import approx_tokens

PAPERS = ["attention", "moshi", "dynamic-chunking"]


def write(tmp_path, text):
    p = tmp_path / "q.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def mk(i, paper, text, page_start, page_end=None):
    return Chunk(chunk_id=f"{paper}:S:{i:04d}", paper_key=paper, setting_id="S", page_start=page_start,
                 page_end=page_end or page_start, section=None, text=text, approx_tokens=approx_tokens(text))


def test_load_questions_accepts_a_valid_file(tmp_path):
    p = write(tmp_path, """
questions:
  - id: a
    text: first question
    answerable: true
    gold: [{paper: attention, pages: [3], evidence: ["dot product"]}]
  - id: b
    text: second question
    answerable: false
""")
    qs = load_questions(p, PAPERS)
    assert [q.id for q in qs] == ["a", "b"]
    assert qs[0].gold[0] == Gold("attention", (3,), ("dot product",))


@pytest.mark.parametrize("body, message", [
    ("questions:\n  - {id: a, text: t, answerable: true}\n", "at least one gold"),
    ("questions:\n  - {id: a, text: t, answerable: false, gold: [{paper: moshi, pages: [1]}]}\n", "no gold"),
    ("questions:\n  - {id: a, text: t, answerable: true, gold: [{paper: nope, pages: [1]}]}\n", "gold.paper"),
    ("questions:\n  - {id: a, text: t, answerable: true, gold: [{paper: moshi}]}\n", "'pages' or 'evidence'"),
    ("questions:\n  - {id: a, text: t, answerable: false}\n  - {id: a, text: u, answerable: false}\n", "duplicate"),
    ("questions:\n  - {id: a, text: t}\n", "answerable"),
    ("questions: []\n", "non-empty"),
])
def test_load_questions_rejects_invalid_files(tmp_path, body, message):
    with pytest.raises(QuestionsError, match=message):
        load_questions(write(tmp_path, body), PAPERS)


def test_missing_questions_file_points_to_the_template(tmp_path):
    with pytest.raises(QuestionsError, match="questions.template.yaml"):
        load_questions(tmp_path / "nope.yaml", PAPERS)


def test_chunk_satisfies_checks_paper_pages_and_evidence():
    c = mk(0, "attention", "We call our attention Scaled Dot-Product Attention", 2, 3)
    assert chunk_satisfies(c, Gold("attention", (3,), ("scaled dot product",)))
    assert chunk_satisfies(c, Gold("attention", (), ("SCALED dot-product attention",)))
    assert chunk_satisfies(c, Gold("attention", (2,), ()))
    assert not chunk_satisfies(c, Gold("moshi", (3,), ()))                 # wrong paper
    assert not chunk_satisfies(c, Gold("attention", (7,), ()))             # page outside range
    assert not chunk_satisfies(c, Gold("attention", (3,), ("softmax",)))   # phrase absent
    assert not chunk_satisfies(c, Gold("attention", (), ("dot prod",)))    # whole words only


def test_evaluate_counts_hits_false_abstains_and_correct_abstains():
    chunks = [
        mk(0, "attention", "scaled dot product attention computes softmax of query key products", 3),
        mk(1, "attention", "positional encoding uses sine and cosine functions", 5),
        mk(2, "moshi", "moshi streams audio tokens with a small codec", 2),
    ]
    setting = RetrievalSetting(id="S", chunk_tokens=100, overlap_tokens=0, k=2, fetch_k=3,
                               rerank="none", section_aware=False)
    retr = Retriever(setting, chunks, abstain_min_coverage=0.5, mmr_lambda=0.7)
    qs = [
        Question("q1", "scaled dot product attention", True, "", (Gold("attention", (3,), ("scaled dot product",)),)),
        Question("q2", "positional encoding", True, "", (Gold("attention", (5,), ("cosine",)),)),
        Question("q3", "dropout rate", True, "", (Gold("attention", (), ("residual dropout",)),)),
        Question("q4", "zzzz quantum gravity", False, "", ()),
    ]
    out = evaluate({"S": retr}, qs)
    m = out["settings"]["S"]
    assert (m["n_answerable"], m["n_unanswerable"]) == (3, 1)
    assert m["hit_any_pct"] == pytest.approx(66.7, abs=0.05)      # q1 and q2 hit, q3 misses
    assert m["false_abstain_pct"] == pytest.approx(33.3, abs=0.05)  # q3 matched nothing
    assert m["correct_abstain_pct"] == 100.0
    assert len(out["per_question"]) == 4
    assert "hit_any%" in format_table(out)
