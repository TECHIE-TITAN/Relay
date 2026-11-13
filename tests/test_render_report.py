from __future__ import annotations

from relay.render_report import render, render_file

MD = """# Title

*An italic line with `code <b>` and **bold**.*

## 1. Section

A paragraph with **bold**, *italic*, `x < y & z` and a literal <tag>.

| A | B |
|---|---|
| 1 | **2** |
| `c` | d & e |

1. first **item**
2. second item

- bullet one

<!-- a hidden
multi-line comment -->

Last line.
"""


def test_headings_paragraphs_inline_and_escaping():
    h = render(MD)
    assert "<h1>Title</h1>" in h and "<h2>1. Section</h2>" in h
    assert "<strong>bold</strong>" in h and "<em>italic</em>" in h
    assert "<code>x &lt; y &amp; z</code>" in h                    # code is escaped, not interpreted
    assert "&lt;tag&gt;" in h and "<tag>" not in h                 # raw html in the text is escaped
    assert "<code>code &lt;b&gt;</code>" in h


def test_tables_lists_and_comments():
    h = render(MD)
    assert "<th>A</th><th>B</th>" in h and "<td>1</td><td><strong>2</strong></td>" in h and "<td>d &amp; e</td>" in h
    assert h.count("<tr>") == 3 and "---" not in h.split("<tbody>")[1].split("</table>")[0]      # the separator row is dropped
    assert "<ol>" in h and "<li>first <strong>item</strong></li>" in h and "<ul>" in h and "<li>bullet one</li>" in h
    assert "hidden" not in h and "multi-line" not in h and "<p>Last line.</p>" in h


def test_page_rule_is_a4_and_render_file_reports_size(tmp_path):
    src, out = tmp_path / "r.md", tmp_path / "r.html"
    src.write_text(MD, encoding="utf-8")
    info = render_file(src, out)
    assert "@page { size: A4;" in out.read_text(encoding="utf-8") and info["words"] > 20 and info["chars"] == len(MD)


def test_the_real_two_page_report_renders_with_every_required_heading_in_order():
    from pathlib import Path
    md = (Path(__file__).resolve().parents[1] / "report" / "report_2page.md")
    if not md.is_file():
        return
    h = render(md.read_text(encoding="utf-8"))
    order = ["1. What I built and why", "2. The real service and my stand-in", "3. Results", "4. Where the time goes",
             "5. When a limit is hit", "Retrieval: quality versus cost", "How this library will mislead a naive retriever",
             "6. Retries", "7. What the person sees", "8. What I&#x27;m unsure about", "Your feedback on Vaani",
             "How I worked with the assistant"]
    pos = [h.find(t) for t in order]
    # heading 8 contains an apostrophe: html.escape(quote=False) leaves it alone
    pos[order.index("8. What I&#x27;m unsure about")] = h.find("8. What I'm unsure about")
    assert all(p >= 0 for p in pos), dict(zip(order, pos))
    assert pos == sorted(pos)
