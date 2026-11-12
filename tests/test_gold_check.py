from relay.eval_retrieval import Gold, Question
from relay.gold_check import check_gold, format_gold_check
from relay.schema import Block, PageRecord


def page(n, text, printed=None, key="attention"):
    return PageRecord(paper_key=key, page=n, blocks=(Block(text),), printed_page=printed)


PAGES = [
    page(6, "3.5 Positional Encoding we must inject some information. to make use of the order", "5"),
    page(7, "length n is smaller than the representation dimensionality d", "6"),
    page(8, "split across two", "7"),
]


def q(*golds):
    return Question("q", "text", True, "", tuple(golds))


def status(question):
    return [r["status"] for r in check_gold(PAGES, [question])]


def test_correct_physical_page_is_ok_and_reports_both_numbers():
    rows = check_gold(PAGES, [q(Gold("attention", (6,), ("make use of the order",)))])
    assert rows[0]["status"] == "OK"
    assert rows[0]["found_pdf_pages"] == [6] and rows[0]["found_printed_pages"] == ["5"]
    assert "pdf p.6 (printed 5)" in format_gold_check(rows)


def test_wrong_physical_page_is_flagged():
    assert status(q(Gold("attention", (7,), ("make use of the order",)))) == ["WRONG PAGE"]


def test_printed_and_physical_numbers_are_not_interchangeable():
    phrase = ("make use of the order",)
    assert status(q(Gold("attention", (), phrase, printed_pages=(5,)))) == ["OK"]        # printed 5 = physical 6
    assert status(q(Gold("attention", (), phrase, printed_pages=(6,)))) == ["WRONG PAGE"]  # printed 6 is physical 7
    assert status(q(Gold("attention", (6,), phrase, printed_pages=(6,)))) == ["WRONG PAGE"]  # both must hold


def test_missing_phrase_is_not_found_and_no_page_constraint_is_ok():
    assert status(q(Gold("attention", (6,), ("text that is nowhere",)))) == ["NOT FOUND"]
    assert status(q(Gold("attention", (), ("make use of the order",)))) == ["OK"]


def test_wrong_paper_does_not_match():
    assert status(q(Gold("moshi", (6,), ("make use of the order",)))) == ["NOT FOUND"]


def test_pages_only_entries_produce_no_rows():
    assert check_gold(PAGES, [q(Gold("attention", (6,), ()))]) == []
