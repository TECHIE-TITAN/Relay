import pytest

from relay.config import RetrievalSetting
from relay.eval_retrieval import (
    Gold, QuestionsError, Question, chunk_satisfies, entry_satisfied, evaluate,
    evidence_fraction, format_table, load_questions,
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


def test_evidence_split_across_retrieved_chunks_counts_for_the_set_but_not_for_one_chunk():
    a = mk(0, "attention", "we use h = 8 parallel attention layers or heads", 5)
    b = mk(1, "attention", "for each of these we use dk = dv = dmodel h = 64", 5)
    g = Gold("attention", (5,), ("h = 8 parallel attention layers", "dk = dv = dmodel h = 64"))
    assert not chunk_satisfies(a, g) and not chunk_satisfies(b, g)
    assert entry_satisfied([a, b], g)
    assert evidence_fraction([a], g) == 0.5
    assert evidence_fraction([mk(2, "moshi", "h = 8 parallel attention layers", 5)], g) == 0.0   # wrong paper
    assert evidence_fraction([mk(3, "attention", "h = 8 parallel attention layers", 9)], g) == 0.0  # wrong page


def test_printed_pages_are_matched_separately_from_physical_pages():
    c = Chunk(chunk_id="a:S:0000", paper_key="attention", setting_id="S", page_start=7, page_end=7,
              section=None, text="positional encodings", approx_tokens=5,
              printed_start="6", printed_end="6")
    assert chunk_satisfies(c, Gold("attention", (7,), ()))
    assert not chunk_satisfies(c, Gold("attention", (6,), ()))                  # 6 is not a physical page here
    assert chunk_satisfies(c, Gold("attention", (), (), printed_pages=(6,)))
    assert not chunk_satisfies(c, Gold("attention", (), (), printed_pages=(7,)))
    assert chunk_satisfies(c, Gold("attention", (7,), (), printed_pages=(6,)))   # both given: both must hold
    assert not chunk_satisfies(c, Gold("attention", (7,), (), printed_pages=(7,)))
    unknown = mk(1, "attention", "positional encodings", 7)                      # no printed number known
    assert not chunk_satisfies(unknown, Gold("attention", (), (), printed_pages=(6,)))


def test_gold_may_use_printed_pages_alone(tmp_path):
    p = write(tmp_path, "questions:\n  - {id: a, text: t, answerable: true, "
                        "gold: [{paper: attention, printed_pages: [6]}]}\n")
    assert load_questions(p, PAPERS)[0].gold[0].printed_pages == (6,)


def test_load_questions_reads_group():
    # tmp_path is not available here, so parse through a temporary file
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "q.yaml"
        p.write_text(
            "questions:\n  - id: a\n    text: t\n    answerable: true\n"
            "    gold:\n      - {paper: moshi, pages: [3], group: g1}\n"
            "      - {paper: moshi, pages: [9], group: g1}\n", encoding="utf-8")
        q = load_questions(p, PAPERS)[0]
    assert [g.group for g in q.gold] == ["g1", "g1"]


def test_evaluate_reports_union_strict_recall_and_alternative_groups():
    chunks = [
        mk(0, "attention", "alpha beta gamma delta epsilon", 1),
        mk(1, "attention", "zeta eta theta iota kappa", 1),
        mk(2, "moshi", "unrelated words only", 2),
    ]
    setting = RetrievalSetting(id="S", chunk_tokens=100, overlap_tokens=0, k=2, fetch_k=3,
                               rerank="none", section_aware=False)
    retr = Retriever(setting, chunks, abstain_min_coverage=0.5, mmr_lambda=0.7)
    split = Question("a", "alpha zeta", True, "",
                     (Gold("attention", (1,), ("beta gamma", "eta theta")),))
    alt = Question("b", "alpha zeta", True, "",
                   (Gold("moshi", (2,), ("nothing here",), group="g"),
                    Gold("attention", (1,), ("beta gamma",), group="g")))
    both = Question("c", "alpha zeta", True, "",
                    (Gold("attention", (1,), ("beta gamma",)), Gold("moshi", (2,), ("nothing here",))))
    rows = {r["id"]: r for r in evaluate({"S": retr}, [split, alt, both])["per_question"]}
    assert rows["a"]["hit_all"] and not rows["a"]["hit_all_single_chunk"] and rows["a"]["evidence_recall"] == 1.0
    assert rows["b"]["hit_all"]                                   # alternatives: one is enough
    assert rows["c"]["hit_any"] and not rows["c"]["hit_all"]      # second required group unmet
    assert rows["c"]["evidence_recall"] == 0.5


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
