"""Export the real Claude Code conversation history of this project to one readable markdown file.

Not cleaned up: every user prompt and every assistant message is exported verbatim, in time order, across all
sessions of the project. What is NOT verbatim, and is said so in the file's header and at each place:
  * secrets are redacted (API keys `gsk_...`, organisation ids `org_...`, `GROQ_API_KEY=...`),
  * very long tool inputs and tool outputs are clipped to a stated number of characters,
  * the assistant's hidden reasoning blocks (`thinking`) are not exported (they are not part of the visible conversation).
The raw session files stay on disk (`~/.claude/projects/...`) and can be supplied on request.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

REDACTIONS = (
    (re.compile(r"gsk_[A-Za-z0-9_\-]{16,}"), "gsk_[REDACTED]"),
    (re.compile(r"org_[A-Za-z0-9]{10,}"), "org_[REDACTED]"),
    (re.compile(r"(GROQ_API_KEY\s*[=:]\s*)\S+"), r"\1[REDACTED]"),
)


def sessions_dir_for(project_root: Path, home: Optional[Path] = None) -> Path:
    """Claude Code names a project's folder after its path with every non-alphanumeric character turned into '-'."""
    return (home or Path.home()) / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(project_root))


def redact(text: str, counter: Optional[dict] = None) -> str:
    for pat, repl in REDACTIONS:
        text, n = pat.subn(repl, text)
        if counter is not None:
            counter["redactions"] = counter.get("redactions", 0) + n
    return text


def clip(text: str, limit: int, counter: Optional[dict] = None) -> str:
    if len(text) <= limit:
        return text
    if counter is not None:
        counter["clipped"] = counter.get("clipped", 0) + 1
    return text[:limit] + f"\n[... {len(text) - limit} more characters omitted from this export ...]"


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(str(b.get("text", "")))
        elif isinstance(b, dict):
            parts.append(f"[{b.get('type')} block]")
    return "\n".join(parts)


def _tool_input(name: str, inp: Any, limit: int, counter: dict) -> str:
    if not isinstance(inp, dict):
        return clip(json.dumps(inp, ensure_ascii=False), limit, counter)
    if name == "Bash" and "command" in inp:
        return clip(str(inp["command"]), limit, counter)
    shown = {}
    for k, v in inp.items():
        shown[k] = clip(v, 400, counter) if isinstance(v, str) and k in ("content", "new_string", "old_string", "prompt") else v
    return clip(json.dumps(shown, ensure_ascii=False), limit, counter)


def _entries(path: Path) -> list:
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") in ("user", "assistant") and isinstance(d.get("message"), dict):
            out.append(d)
    return out


def export(session_files: Sequence[Path], out_path: Path, *, max_tool_chars: int = 1500,
           only_sessions: Optional[Sequence[str]] = None) -> dict:
    """Write the markdown file; returns counts."""
    counter: dict = {}
    sessions = []
    for p in sorted(session_files):
        if only_sessions and p.stem not in only_sessions:
            continue
        es = _entries(p)
        if es:
            sessions.append((min(e.get("timestamp", "") for e in es), p, es))
    sessions.sort(key=lambda s: s[0])
    body: list = []
    n_user = n_assistant = n_calls = n_results = n_thinking = 0
    for _first, p, es in sessions:
        body += [f"\n---\n## Session {p.stem}\n"]
        for e in sorted(es, key=lambda e: e.get("timestamp", "")):
            role, ts = e["message"].get("role", e["type"]), e.get("timestamp", "")
            content = e["message"].get("content")
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text" and str(b.get("text", "")).strip():
                    label = "USER" if role == "user" else "ASSISTANT"
                    n_user += role == "user"
                    n_assistant += role != "user"
                    body.append(f"### {label}  `{ts}`\n\n{redact(str(b['text']), counter)}\n")
                elif t == "tool_use":
                    n_calls += 1
                    body.append(f"**Tool call** `{b.get('name')}` `{ts}`\n\n```\n"
                                f"{redact(_tool_input(str(b.get('name')), b.get('input'), max_tool_chars, counter), counter)}\n```\n")
                elif t == "tool_result":
                    n_results += 1
                    body.append(f"**Tool result** `{ts}`\n\n```\n"
                                f"{redact(clip(_result_text(b.get('content')), max_tool_chars, counter), counter)}\n```\n")
                elif t == "thinking":
                    n_thinking += 1
    header = [
        "# Conversation history with the assistant (Claude Code), exported from the real session files", "",
        f"Exported {datetime.now(timezone.utc).isoformat(timespec='seconds')}. Sessions: "
        + ", ".join(f"`{p.stem}` (from {first[:19]})" for first, p, _ in sessions) + ".", "",
        f"Contents: {n_user} user messages, {n_assistant} assistant messages, {n_calls} tool calls, {n_results} tool results.",
        "Not cleaned up. Not verbatim only where stated here: "
        f"{counter.get('redactions', 0)} secret-looking strings redacted; {counter.get('clipped', 0)} long tool inputs/outputs clipped "
        f"to {max_tool_chars} characters (marked where it happened); {n_thinking} hidden reasoning blocks not exported.", "",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(header + body), encoding="utf-8")
    return {"sessions": [p.stem for _f, p, _e in sessions], "user": n_user, "assistant": n_assistant, "tool_calls": n_calls,
            "tool_results": n_results, "thinking_skipped": n_thinking, **counter, "out": str(out_path)}
