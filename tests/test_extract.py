from relay.extract import (
    RawBlock, body_left_margin, build_page_records, clean_text, dehyphenate_join,
    drop_margin_noise, find_arxiv_stamp, is_heading,
)

H = 800.0  # page height used by the fixtures


def rb(page, text, *, size=10.0, bold=0.0, lines=1, y0=300.0, y1=320.0, rotated=False, x0=0.0):
    return RawBlock(page=page, text=text, size=size, bold_frac=bold, n_lines=lines,
                    y0=y0, y1=y1, page_height=H, rotated=rotated, x0=x0)


def test_clean_text_fixes_ligatures_and_control_characters():
    assert clean_text("ﬁne­  tuning\x00") == "fine tuning"


def test_dehyphenate_join():
    assert dehyphenate_join(["informa-", "tion retrieval"]) == "information retrieval"
    assert dehyphenate_join(["well-", "Known method"]) == "well-Known method"
    assert dehyphenate_join(["plain", "lines"]) == "plain lines"
    assert dehyphenate_join(["", "  ", "x"]) == "x"


def test_find_arxiv_stamp_forward_and_reversed():
    assert find_arxiv_stamp("foo arXiv:1706.03762v7 [cs.CL] 2 Aug 2023") == ("1706.03762", 7)
    assert find_arxiv_stamp("3202 guA 2 ]LC.sc[ 7v26730.6071:viXra") == ("1706.03762", 7)
    assert find_arxiv_stamp("no stamp here") is None


def test_is_heading_rules():
    body = 10.0
    assert is_heading(rb(1, "Introduction", size=14), body)                       # larger font
    assert is_heading(rb(1, "3.1 Encoder and Decoder Stacks", bold=1.0), body)   # bold + numbered
    assert is_heading(rb(1, "References", bold=1.0), body)
    assert not is_heading(rb(1, "Scaled attention is computed as follows."), body)
    assert not is_heading(rb(1, "A heading that ends with a period.", size=14), body)
    assert not is_heading(rb(1, "x " * 40, size=14), body)                        # too long
    assert not is_heading(rb(1, "Model", bold=1.0), body)                         # bold table cell, not numbered
    assert not is_heading(rb(1, "Introduction", size=14, rotated=True), body)


def test_drop_margin_noise_removes_repeated_footers_page_numbers_and_rotated_text():
    blocks = []
    for p in range(1, 11):
        blocks.append(rb(p, f"Preprint under review {p}", y0=770.0, y1=790.0))   # repeated footer
        blocks.append(rb(p, f"{p}", y0=775.0, y1=790.0))                         # bare page number
        blocks.append(rb(p, f"Body paragraph number {p}"))                       # real content
    blocks.append(rb(1, "arXiv:1706.03762v7 [cs.CL]", rotated=True))
    kept, dropped = drop_margin_noise(blocks, n_pages=10)
    assert [b.text for b in kept] == [f"Body paragraph number {p}" for p in range(1, 11)]
    assert dropped[1] == 3 and dropped[2] == 2


def test_build_page_records_marks_references_and_resumes_at_appendix():
    body = 10.0
    kept = [
        rb(1, "1 Introduction", size=14), rb(1, "Some body text."),
        rb(2, "References", size=14), rb(2, "[1] A. Author. A paper."),
        rb(3, "A Training details", size=14), rb(3, "More training details follow."),
    ]
    records, meta = build_page_records("p", kept, body, n_pages=3, dropped_by_page={})
    flags = [(b.text, b.kind, b.in_references) for r in records for b in r.blocks]
    assert flags == [
        ("1 Introduction", "heading", False),
        ("Some body text.", "body", False),
        ("References", "heading", True),
        ("[1] A. Author. A paper.", "body", True),
        ("A Training details", "heading", False),
        ("More training details follow.", "body", False),
    ]
    assert meta["references_start_page"] == 2


def test_figure_label_clusters_and_stray_letters_are_not_headings():
    kept = [
        rb(3, "Chunking", size=14), rb(3, "Dechunking", size=14), rb(3, "M", size=14),
        rb(3, "Body text of the introduction continues here."),
    ]
    _records, meta = build_page_records("p", kept, 10.0, n_pages=3, dropped_by_page={})
    assert meta["headings"] == []


def test_body_left_margin_is_the_most_common_paragraph_edge():
    blocks = [
        rb(1, "x " * 40, lines=5, x0=72.0),
        rb(2, "y " * 40, lines=5, x0=72.0),
        rb(3, "z " * 10, lines=3, x0=150.0),
        rb(3, "label", lines=1, x0=300.0),      # single-line blocks are ignored
    ]
    assert body_left_margin(blocks) == 72.0
    assert body_left_margin([rb(1, "label", lines=1)]) is None


def test_weak_heading_away_from_the_left_margin_is_rejected():
    kept = [
        rb(3, "Dechunking", size=14, x0=250.0), rb(3, "Body paragraph text."),
        rb(4, "Attention Visualizations", size=14, x0=72.0), rb(4, "Body paragraph text."),
    ]
    _records, meta = build_page_records("p", kept, 10.0, n_pages=4, dropped_by_page={}, body_x0=72.0)
    assert [t for _p, t in meta["headings"]] == ["Attention Visualizations"]


def test_isolated_unnumbered_heading_is_kept_and_ends_the_reference_list():
    kept = [
        rb(1, "References", size=14), rb(1, "[1] A. Author. A paper."),
        rb(2, "Attention Visualizations", size=14),
        rb(2, "Figure 3: An example caption of an attention head."),
    ]
    records, meta = build_page_records("p", kept, 10.0, n_pages=2, dropped_by_page={})
    assert [t for _p, t in meta["headings"]] == ["References", "Attention Visualizations"]
    assert records[1].blocks[0].in_references is False
    assert records[1].blocks[1].in_references is False
