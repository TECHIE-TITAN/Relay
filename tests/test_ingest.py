"""Ingest tests use an in-memory HTTP transport and a generated PDF: no network."""
import hashlib
import json

import httpx
import pytest

from relay.extract import fitz
from relay.ingest import IngestError, ingest


def make_pdf(stamp_text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), stamp_text)
    data = doc.tobytes()
    doc.close()
    return data


def client_for(body: bytes, counter: list, content_type="application/pdf", status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        counter.append(str(request.url))
        return httpx.Response(status, content=body, headers={"content-type": content_type})
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


NO_SLEEP = lambda s: None  # noqa: E731


def test_downloads_verifies_version_and_writes_manifest(cfg):
    calls: list = []
    client = client_for(make_pdf("arXiv:1706.03762v7 [cs.CL] 2 Aug 2023"), calls)
    result = ingest(cfg, client=client, sleep=NO_SLEEP)
    assert calls == ["https://arxiv.org/pdf/1706.03762v7"]
    assert result.statuses == {"attention": "downloaded"}
    entry = result.entries[0]
    assert entry["stamp_verified"] is True and entry["stamp_found"] == "arXiv:1706.03762v7"
    dest = cfg.paths.papers_dir / "attention_1706.03762v7.pdf"
    assert entry["sha256"] == hashlib.sha256(dest.read_bytes()).hexdigest()
    manifest = json.loads(cfg.paths.manifest.read_text(encoding="utf-8"))
    assert manifest["papers"][0]["requested_version"] == 7


def test_second_run_downloads_nothing(cfg):
    calls: list = []
    client = client_for(make_pdf("arXiv:1706.03762v7"), calls)
    ingest(cfg, client=client, sleep=NO_SLEEP)
    result = ingest(cfg, client=client, sleep=NO_SLEEP)
    assert len(calls) == 1
    assert result.statuses == {"attention": "cached"}


def test_force_downloads_again(cfg):
    calls: list = []
    client = client_for(make_pdf("arXiv:1706.03762v7"), calls)
    ingest(cfg, client=client, sleep=NO_SLEEP)
    ingest(cfg, force=True, client=client, sleep=NO_SLEEP)
    assert len(calls) == 2


def test_wrong_version_is_rejected_and_file_removed(cfg):
    client = client_for(make_pdf("arXiv:1706.03762v6"), [])
    with pytest.raises(IngestError, match="requested 1706.03762v7"):
        ingest(cfg, client=client, sleep=NO_SLEEP)
    assert not (cfg.paths.papers_dir / "attention_1706.03762v7.pdf").exists()


def test_missing_stamp_fails_unless_explicitly_allowed(cfg):
    pdf = make_pdf("no stamp on this page")
    with pytest.raises(IngestError, match="allow-missing-stamp"):
        ingest(cfg, client=client_for(pdf, []), sleep=NO_SLEEP)
    result = ingest(cfg, allow_missing_stamp=True, client=client_for(pdf, []), sleep=NO_SLEEP)
    assert result.entries[0]["stamp_verified"] is False and result.entries[0]["stamp_found"] is None


def test_non_pdf_and_http_errors_are_rejected(cfg):
    with pytest.raises(IngestError, match="did not return a PDF"):
        ingest(cfg, client=client_for(b"<html>nope</html>", [], "text/html"), sleep=NO_SLEEP)
    with pytest.raises(IngestError, match="HTTP 404"):
        ingest(cfg, client=client_for(b"", [], status=404), sleep=NO_SLEEP)
    assert list(cfg.paths.papers_dir.glob("*.part")) == []     # no leftovers
