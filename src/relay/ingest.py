"""Download the three pinned arXiv PDFs and prove their versions.

This is the only module in Phase 0/1 that touches the network, and it talks
only to arxiv.org. It is idempotent: a PDF whose checksum matches the manifest
(and whose version stamp was verified) is not downloaded again.

Version proof: arXiv stamps each PDF with 'arXiv:<id>v<version>' in the margin
of page 1. The stamp found in the file must equal the requested version, or
the download is rejected. If a PDF has no readable stamp the run fails unless
--allow-missing-stamp is given, in which case the manifest records
`stamp_verified: false` so the gap is visible.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from .config import Config, PaperSpec
from .extract import read_pdf_stamp
from .schema import RelayError

# Generic client name: no personal details are sent to a third party.
USER_AGENT = "relay-assignment/0.1 (single polite fetch of three papers)"
POLITE_DELAY_S = 3.0   # arXiv asks automated clients to wait between requests


class IngestError(RelayError):
    """Download or verification failed."""


@dataclass
class IngestResult:
    entries: list[dict[str, Any]]
    statuses: dict[str, str]   # paper key -> "downloaded" | "cached"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _load_manifest(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise IngestError(f"{path} is not valid JSON ({e}); delete it and re-run") from e
    return {p["key"]: p for p in data.get("papers", [])}


def _download(client: httpx.Client, url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        with client.stream("GET", url) as r:
            if r.status_code != 200:
                raise IngestError(f"GET {url} returned HTTP {r.status_code}")
            first = True
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes():
                    if first:
                        if not chunk.startswith(b"%PDF"):
                            raise IngestError(f"GET {url} did not return a PDF")
                        first = False
                    f.write(chunk)
            if first:
                raise IngestError(f"GET {url} returned an empty body")
        tmp.replace(dest)
    except httpx.HTTPError as e:
        raise IngestError(f"network error fetching {url}: {e}") from e
    finally:
        if tmp.exists():
            tmp.unlink()


def _entry(paper: PaperSpec, dest: Path, stamp: Optional[tuple[str, int]], pages: int) -> dict[str, Any]:
    return {
        "key": paper.key,
        "arxiv_id": paper.arxiv_id,
        "requested_version": paper.version,
        "title": paper.title,
        "url": paper.pdf_url,
        "file": dest.name,
        "sha256": sha256_file(dest),
        "size_bytes": dest.stat().st_size,
        "pages": pages,
        "stamp_found": f"arXiv:{stamp[0]}v{stamp[1]}" if stamp else None,
        "stamp_verified": stamp is not None,
        "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def ingest(
    cfg: Config,
    *,
    force: bool = False,
    allow_missing_stamp: bool = False,
    client: Optional[httpx.Client] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> IngestResult:
    old = _load_manifest(cfg.paths.manifest)
    own_client = client is None
    if client is None:
        client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
        )
    entries: list[dict[str, Any]] = []
    statuses: dict[str, str] = {}
    downloaded_any = False
    try:
        for paper in cfg.papers:
            dest = cfg.paths.papers_dir / paper.filename
            prev = old.get(paper.key)
            if (
                not force
                and prev is not None
                and dest.is_file()
                and prev.get("stamp_verified")
                and prev.get("arxiv_id") == paper.arxiv_id
                and prev.get("requested_version") == paper.version
                and prev.get("sha256") == sha256_file(dest)
            ):
                entries.append(prev)
                statuses[paper.key] = "cached"
                continue

            if downloaded_any:
                sleep(POLITE_DELAY_S)
            _download(client, paper.pdf_url, dest)
            downloaded_any = True

            try:
                stamp, pages = read_pdf_stamp(dest)
            except Exception as e:  # unreadable PDF
                dest.unlink(missing_ok=True)
                raise IngestError(f"{paper.key}: downloaded file is not a readable PDF ({e})") from e

            if stamp is None:
                if not allow_missing_stamp:
                    dest.unlink(missing_ok=True)
                    raise IngestError(
                        f"{paper.key}: no 'arXiv:<id>v<n>' stamp found in the first 3 pages, so the "
                        f"version cannot be verified. Re-run with --allow-missing-stamp to accept it "
                        f"(recorded as unverified)."
                    )
            elif stamp != (paper.arxiv_id, paper.version):
                dest.unlink(missing_ok=True)
                raise IngestError(
                    f"{paper.key}: requested {paper.versioned_id} but the file says "
                    f"arXiv:{stamp[0]}v{stamp[1]}. File removed."
                )
            entries.append(_entry(paper, dest, stamp, pages))
            statuses[paper.key] = "downloaded"
    finally:
        if own_client:
            client.close()

    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "papers": entries,
    }
    cfg.paths.manifest.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return IngestResult(entries=entries, statuses=statuses)
