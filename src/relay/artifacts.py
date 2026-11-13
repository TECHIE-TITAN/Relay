"""On-disk layout and schema of run artifacts (shared by the runner and, later, the dashboard).

results/runs/<run_id>/
    manifest.json   scenario, source, limits used with provenance, config, start/end
    events.jsonl    every event (see tracing.py)
    results.csv     exactly the brief's columns, one row per request
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .schema import RelayError, read_jsonl

RESULT_COLUMNS = (
    "request_id", "scenario", "source", "start_time", "first_token_ms", "total_ms", "status",
    "retries", "tokens_in", "tokens_out", "retrieval_setting", "answer_grade",
)
STATUSES = ("ok", "rate_limited", "timeout", "error")
SCHEMA_VERSION = 1
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$")


@dataclass(frozen=True)
class RunPaths:
    root: Path

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def events(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def results(self) -> Path:
        return self.root / "results.csv"


def runs_dir(results_dir: Path) -> Path:
    return results_dir / "runs"


def new_run_id(scenario: str, when: datetime = None) -> str:   # type: ignore[assignment]
    when = when or datetime.now(timezone.utc)
    return f"{scenario}_{when.strftime('%Y%m%dT%H%M%SZ')}"


def run_paths(results_dir: Path, run_id: str) -> RunPaths:
    """Resolve a run directory. Rejects ids that could escape results/runs."""
    if not _RUN_ID_RE.match(run_id):
        raise RelayError(f"invalid run id {run_id!r}")
    return RunPaths(runs_dir(results_dir) / run_id)


def list_runs(results_dir: Path) -> list:
    base = runs_dir(results_dir)
    if not base.is_dir():
        return []
    dirs = [p for p in base.iterdir() if p.is_dir() and (p / "manifest.json").is_file()]
    return [p.name for p in sorted(dirs, key=lambda p: (p.stat().st_mtime, p.name))]   # oldest first


def write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": SCHEMA_VERSION, **manifest}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def read_manifest(path: Path) -> dict:
    if not path.is_file():
        raise RelayError(f"{path} not found")
    return json.loads(path.read_text(encoding="utf-8"))


def read_events(path: Path) -> list:
    return list(read_jsonl(path))


def iter_events(path: Path) -> Iterator[dict]:
    return read_jsonl(path)


def write_results_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(RESULT_COLUMNS), extrasaction="raise")
        w.writeheader()
        for r in rows:
            if r.get("status") not in STATUSES:
                raise RelayError(f"row {r.get('request_id')}: status {r.get('status')!r} not in {STATUSES}")
            w.writerow({k: ("" if r.get(k) is None else r[k]) for k in RESULT_COLUMNS})
    return len(rows)


def read_results_csv(path: Path) -> list:
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))
