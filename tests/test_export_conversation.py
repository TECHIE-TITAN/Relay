from __future__ import annotations

import json
from pathlib import Path

from relay.export_conversation import export, redact, sessions_dir_for

KEY = "gsk_abcdefghijklmnopqrstuvwxyz123456"


def entry(kind, ts, content):
    return {"type": kind, "timestamp": ts, "message": {"role": kind, "content": content}}


def write(path: Path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries + [{"type": "attachment", "x": 1}]) + "\n", encoding="utf-8")


def test_export_is_verbatim_in_time_order_redacted_and_says_what_it_changed(tmp_path):
    write(tmp_path / "later.jsonl", [
        entry("user", "2026-11-13T10:00:00Z", "second session prompt"),
        entry("assistant", "2026-11-13T10:00:05Z", [{"type": "text", "text": "second session answer"}]),
    ])
    write(tmp_path / "earlier.jsonl", [
        entry("user", "2026-11-11T09:00:00Z", f"my key is {KEY} and org org_01m77rec8vesj8v4r411tgqwsp, keep it\nline two"),
        entry("assistant", "2026-11-11T09:00:02Z", [
            {"type": "thinking", "thinking": "SECRET HIDDEN REASONING"},
            {"type": "text", "text": "I will run a command."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo hello"}},
            {"type": "tool_use", "name": "Write", "input": {"file_path": "/x.py", "content": "y" * 1000}}]),
        entry("user", "2026-11-11T09:00:03Z", [{"type": "tool_result", "content": "z" * 3000}]),
    ])
    out = tmp_path / "out" / "history.md"
    res = export(sorted(tmp_path.glob("*.jsonl")), out, max_tool_chars=1500)
    md = out.read_text(encoding="utf-8")
    assert md.index("earlier") < md.index("later")                           # sessions in time order
    assert "line two" in md and "I will run a command." in md and "second session answer" in md      # verbatim text kept
    assert KEY not in md and "org_01m77rec8vesj8v4r411tgqwsp" not in md and "gsk_[REDACTED]" in md and "org_[REDACTED]" in md
    assert "SECRET HIDDEN REASONING" not in md                               # hidden reasoning is not exported
    assert "echo hello" in md
    assert "more characters omitted from this export" in md                   # clipping is marked where it happens
    assert md.count("z" * 1500) == 1 and "z" * 1501 not in md
    assert res["user"] == 2 and res["assistant"] == 2 and res["tool_calls"] == 2 and res["tool_results"] == 1
    assert res["thinking_skipped"] == 1 and res["redactions"] >= 2 and res["clipped"] >= 2
    assert "not exported" in md and "redacted" in md.split("---")[0]         # the header says what is not verbatim


def test_only_chosen_sessions_are_exported(tmp_path):
    write(tmp_path / "a.jsonl", [entry("user", "2026-11-11T09:00:00Z", "from a")])
    write(tmp_path / "b.jsonl", [entry("user", "2026-11-11T10:00:00Z", "from b")])
    res = export(sorted(tmp_path.glob("*.jsonl")), tmp_path / "o.md", only_sessions=["b"])
    assert res["sessions"] == ["b"] and "from b" in (tmp_path / "o.md").read_text(encoding="utf-8")
    assert "from a" not in (tmp_path / "o.md").read_text(encoding="utf-8")


def test_session_folder_name_follows_the_claude_code_convention():
    root = Path("/Users/harishkumar/Desktop/WORKSPACE/Project_Relay/Relay")
    assert sessions_dir_for(root, home=Path("/h")).name == "-Users-harishkumar-Desktop-WORKSPACE-Project-Relay-Relay"
    assert redact(f"GROQ_API_KEY={KEY}") == "GROQ_API_KEY=[REDACTED]"
