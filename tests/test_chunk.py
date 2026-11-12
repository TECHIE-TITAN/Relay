import pytest

from relay.chunk import _windows, _Word, build_chunks, build_all
from relay.config import RetrievalSetting
from relay.schema import Block, PageRecord
from relay.textutil import word_cost


def setting(size, overlap, *, section_aware=False, sid="S"):
    return RetrievalSetting(id=sid, chunk_tokens=size, overlap_tokens=overlap, k=2, fetch_k=2,
                            rerank="none", section_aware=section_aware)


def words(prefix, n):
    # every word is 4 characters, so every word costs exactly (4 + 1) / 4 = 1.25 tokens
    return [f"{prefix}{i:03d}" for i in range(n)]


def page(n, blocks, key="p"):
    return PageRecord(paper_key=key, page=n, blocks=tuple(blocks))


def test_windows_overlap_exactly_and_lose_no_words():
    ws = [_Word(w, 1, None) for w in words("w", 100)]
    wins = _windows(ws, size=10, overlap=2.5)
    assert wins[0] == (0, 8)                       # 8 words * 1.25 = 10 tokens
    assert wins[1][0] == 6                         # next window starts 2 words (2.5 tokens) earlier
    assert wins[0][1] - wins[1][0] == 2
    assert wins[-1][1] == 100                      # reaches the end
    covered = set()
    for s, e in wins:
        covered.update(range(s, e))
    assert covered == set(range(100))              # no word lost


def test_chunk_sizes_stay_within_documented_bounds():
    ws = [_Word(w, 1, None) for w in words("w", 100)]
    for s, e in _windows(ws, size=10, overlap=2.5)[:-1]:
        cost = sum(word_cost(w.text) for w in ws[s:e])
        assert 10 <= cost <= 1.25 * 10 + 1.25      # size .. 1.25 * size (+ one word)


def test_windows_always_make_progress_even_with_huge_overlap():
    ws = [_Word("w" * 400, 1, None) for _ in range(5)]   # each word costs ~100 tokens
    wins = _windows(ws, size=10, overlap=9)
    starts = [s for s, _ in wins]
    assert starts == sorted(set(starts)) and wins[-1][1] == 5


def test_chunks_carry_physical_and_printed_pages_separately():
    p1 = PageRecord(paper_key="p", page=2, blocks=(Block(" ".join(words("a", 12))),), printed_page="1")
    p2 = PageRecord(paper_key="p", page=3, blocks=(Block(" ".join(words("b", 12))),), printed_page="2")
    chunks = build_chunks([p1, p2], setting(30, 0))
    assert chunks[0].page_start == 2 and chunks[0].printed_start == "1"
    assert chunks[-1].page_end == 3 and chunks[-1].printed_end == "2"
    from relay.schema import Chunk
    assert Chunk.from_dict(chunks[0].to_dict()) == chunks[0]          # round-trips through JSON dicts
    old = chunks[0].to_dict()
    old.pop("printed_start"); old.pop("printed_end")                  # files written before this field existed
    assert Chunk.from_dict(old).printed_start is None


def test_short_segment_gives_one_chunk():
    pages = [page(1, [Block(" ".join(words("w", 5)))])]
    chunks = build_chunks(pages, setting(100, 10))
    assert len(chunks) == 1 and chunks[0].text == " ".join(words("w", 5))


def test_chunks_are_deterministic_and_ids_stable():
    pages = [page(1, [Block(" ".join(words("w", 120)))])]
    a = build_chunks(pages, setting(20, 5))
    b = build_chunks(pages, setting(20, 5))
    assert a == b
    assert [c.chunk_id for c in a] == [f"p:S:{i:04d}" for i in range(len(a))]


def test_pages_and_section_attribution():
    pages = [
        page(1, [Block("1 Intro", "heading"), Block(" ".join(words("a", 40)))]),
        page(2, [Block(" ".join(words("b", 40)))]),
    ]
    chunks = build_chunks(pages, setting(30, 0))
    assert chunks[0].page_start == 1
    assert chunks[-1].page_end == 2
    assert any(c.page_start == 1 and c.page_end == 2 for c in chunks)   # a chunk spans the page break
    assert all(c.section == "1 Intro" for c in chunks)


def test_section_aware_chunks_never_cross_a_heading():
    pages = [page(1, [
        Block("1 Intro", "heading"), Block(" ".join(words("a", 80))),
        Block("2 Methods", "heading"), Block(" ".join(words("b", 80))),
    ])]
    chunks = build_chunks(pages, setting(40, 5, section_aware=True))
    assert len(chunks) >= 4
    for c in chunks:
        assert not ("a000" in c.text and "b000" in c.text)
        assert not (" a0" in f" {c.text}" and " b0" in f" {c.text}")
    assert {c.section for c in chunks} == {"1 Intro", "2 Methods"}


def test_non_section_aware_chunks_may_cross_headings():
    pages = [page(1, [
        Block("1 Intro", "heading"), Block(" ".join(words("a", 30))),
        Block("2 Methods", "heading"), Block(" ".join(words("b", 30))),
    ])]
    chunks = build_chunks(pages, setting(40, 5, section_aware=False))
    assert any(" a0" in f" {c.text}" and " b0" in f" {c.text}" for c in chunks)


def test_tiny_heading_only_segment_is_merged_into_the_next():
    pages = [page(1, [
        Block("3 Model", "heading"),
        Block("3.1 Encoder", "heading"), Block(" ".join(words("a", 80))),
    ])]
    chunks = build_chunks(pages, setting(40, 5, section_aware=True))
    assert chunks[0].text.startswith("3 Model 3.1 Encoder")


def test_reference_blocks_are_excluded():
    pages = [page(1, [
        Block("body text here"),
        Block("References", "heading", in_references=True),
        Block("[1] hidden reference entry", in_references=True),
    ])]
    text = " ".join(c.text for c in build_chunks(pages, setting(100, 10)))
    assert "body text here" in text and "hidden" not in text and "References" not in text


def test_build_chunks_rejects_mixed_papers_and_build_all_keeps_papers_apart():
    a = page(1, [Block("alpha " * 30)], key="x")
    b = page(1, [Block("beta " * 30)], key="y")
    with pytest.raises(ValueError):
        build_chunks([a, b], setting(100, 10))
    chunks = build_all([a, b], ["x", "y"], setting(100, 10))
    assert {c.paper_key for c in chunks} == {"x", "y"}
    assert all(("alpha" in c.text) == (c.paper_key == "x") for c in chunks)
