"""Render the 2-page write-up (markdown subset) to a self-contained HTML file with print CSS (A4).

Open the HTML in a browser and print to PDF to see the real page count; the page rule is `@page {size: A4; margin: 9mm}`.
Supported markdown: `#`/`##`/`###` headings, pipe tables, `1.` lists, `- ` lists, **bold**, *italic*, `code`, one block per line.
No third-party package is needed.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

CSS = """
@page { size: A4; margin: 9mm; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; font-size: 7.7pt; line-height: 1.2; color: #111; margin: 0; }
h1 { font-size: 11pt; margin: 0 0 2pt; } h2 { font-size: 8.6pt; margin: 4pt 0 1.5pt; border-bottom: 0.4pt solid #888; }
p { margin: 0 0 2pt; text-align: left; } ol, ul { margin: 0 0 2pt 11pt; padding: 0; } li { margin: 0 0 1pt; }
code { font-family: Menlo, Consolas, monospace; font-size: 0.92em; }
table { border-collapse: collapse; width: 100%; margin: 1.5pt 0 3pt; font-size: 6.4pt; line-height: 1.12; page-break-inside: avoid; }
th, td { border: 0.4pt solid #999; padding: 0.6pt 2pt; vertical-align: top; } th { background: #eee; text-align: left; }
"""


def _inline(text: str) -> str:
    codes: list = []

    def stash(m: "re.Match[str]") -> str:
        codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00{len(codes) - 1}\x00"

    s = re.sub(r"`([^`]+)`", stash, text)
    s = html.escape(s, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], s)


def _cells(line: str) -> list:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def render(md: str) -> str:
    out: list = []
    lines = md.splitlines()
    i = 0
    open_list = None
    in_comment = False

    def close_list() -> None:
        nonlocal open_list
        if open_list:
            out.append(f"</{open_list}>")
            open_list = None

    while i < len(lines):
        line = lines[i].rstrip()
        if "<!--" in line:
            in_comment = True
        if in_comment:
            if "-->" in line:
                in_comment = False
            i += 1
            continue
        if not line.strip():
            close_list()
            i += 1
            continue
        if line.startswith("|"):
            close_list()
            block = []
            while i < len(lines) and lines[i].startswith("|"):
                block.append(lines[i])
                i += 1
            head, body = _cells(block[0]), [_cells(b) for b in block[2:]]
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead><tbody>")
            for r in body:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
            out.append("</tbody></table>")
            continue
        m = re.match(r"^(#{1,3})\s+(.*)$", line)
        if m:
            close_list()
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
        elif re.match(r"^\d+\.\s+", line):
            if open_list != "ol":
                close_list()
                out.append("<ol>")
                open_list = "ol"
            item = re.sub(r"^\d+\.\s+", "", line)          # (Python 3.9: no backslash inside an f-string expression)
            out.append(f"<li>{_inline(item)}</li>")
        elif line.startswith("- "):
            if open_list != "ul":
                close_list()
                out.append("<ul>")
                open_list = "ul"
            out.append(f"<li>{_inline(line[2:])}</li>")
        else:
            close_list()
            out.append(f"<p>{_inline(line)}</p>")
        i += 1
    close_list()
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>The Relay: write-up</title>"
            f"<style>{CSS}</style></head><body>\n" + "\n".join(out) + "\n</body></html>\n")


def render_file(md_path: Path, html_path: Path) -> dict:
    md = md_path.read_text(encoding="utf-8")
    html_path.write_text(render(md), encoding="utf-8")
    words = len(re.findall(r"\S+", md))
    return {"words": words, "chars": len(md), "html": str(html_path)}
