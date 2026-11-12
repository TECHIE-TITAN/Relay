from relay.config import PaperSpec, RetrievalSetting
from relay.retrieve import Retriever, detect_papers
from relay.schema import Chunk
from relay.textutil import approx_tokens


def mk(i, paper, text, page=1):
    return Chunk(chunk_id=f"{paper}:S:{i:04d}", paper_key=paper, setting_id="S", page_start=page,
                 page_end=page, section=None, text=text, approx_tokens=approx_tokens(text))


def setting(rerank="none", k=2, fetch_k=4):
    return RetrievalSetting(id="S", chunk_tokens=100, overlap_tokens=10, k=k, fetch_k=fetch_k,
                            rerank=rerank, section_aware=False)


CHUNKS = [
    mk(0, "attention", "multi head attention allows the model to attend to several positions", 3),
    mk(1, "attention", "positional encoding uses sine and cosine functions of different frequencies", 5),
    mk(2, "moshi", "the codec encodes speech into discrete audio tokens at a low frame rate", 2),
    mk(3, "dynamic-chunking", "dynamic chunking learns segment boundaries end to end", 4),
]


def retriever(rerank="none", k=2, fetch_k=4):
    return Retriever(setting(rerank, k, fetch_k), CHUNKS, abstain_min_coverage=0.5, mmr_lambda=0.7)


def test_known_item_query_returns_the_right_chunk_first():
    res = retriever().search("audio codec tokens")
    assert res.hits[0].chunk.chunk_id == "moshi:S:0002"
    assert not res.abstain and res.coverage == 1.0
    assert [h.rank for h in res.hits] == list(range(1, len(res.hits) + 1))


def test_paper_filter_restricts_results():
    res = retriever().search("attention positions codec", paper_keys=["moshi"])
    assert {h.chunk.paper_key for h in res.hits} == {"moshi"}


def test_nonsense_query_abstains_with_no_hits():
    res = retriever().search("zzxq qqqq")
    assert res.abstain and res.abstain_reason == "no_match" and res.hits == ()


def test_empty_query_abstains():
    res = retriever().search("the of and")        # stopwords only
    assert res.abstain and res.abstain_reason == "empty_query"


def test_low_coverage_abstains_but_still_returns_hits_for_inspection():
    # only "attention" occurs in the corpus: coverage = 1 / 4 = 0.25 < 0.5
    res = retriever().search("attention bleu score wmt")
    assert res.abstain and res.abstain_reason == "low_coverage"
    assert res.hits and abs(res.coverage - 0.25) < 1e-9


def test_k_limits_the_number_of_hits():
    # "attention" matches chunk 0 and "encoding" matches chunk 1
    assert len(retriever(k=2).search("attention encoding").hits) == 2
    assert len(retriever(k=1).search("attention encoding").hits) == 1


def test_search_is_deterministic():
    r = retriever()
    a = [h.chunk.chunk_id for h in r.search("attention encoding").hits]
    assert a == [h.chunk.chunk_id for h in r.search("attention encoding").hits]


def test_mmr_prefers_a_distinct_chunk_over_a_duplicate():
    chunks = [
        mk(0, "p", "alpha beta gamma delta"),
        mk(1, "p", "delta gamma beta alpha"),     # identical token set -> Jaccard 1.0
        mk(2, "p", "xray yankee"),
    ]
    r = Retriever(setting("mmr", k=2, fetch_k=3), chunks, abstain_min_coverage=0.0, mmr_lambda=0.7)
    # second pick: duplicate = 0.7 * 0.95 - 0.3 * 1.0 = 0.365 ; distinct = 0.7 * 0.6 - 0 = 0.42
    picked = r._mmr([(0, 10.0), (1, 9.5), (2, 6.0)], 2)
    assert [i for i, _ in picked] == [0, 2]


def test_detect_papers_is_conservative():
    papers = [
        PaperSpec("attention", "1706.03762", 7, "Attention Is All You Need"),
        PaperSpec("moshi", "2410.00037", 1, "Moshi: a speech-text foundation model for real-time dialogue"),
        PaperSpec("dynamic-chunking", "2507.07955", 2,
                  "Dynamic Chunking for End-to-End Hierarchical Sequence Modeling"),
    ]
    assert detect_papers("How does Moshi handle overlapping speech?", papers) == ["moshi"]
    assert detect_papers("In the Attention Is All You Need paper, how many heads?", papers) == ["attention"]
    assert detect_papers("What does dynamic chunking predict?", papers) == ["dynamic-chunking"]
    assert detect_papers("How many attention heads are used?", papers) == []   # bare word is ambiguous
