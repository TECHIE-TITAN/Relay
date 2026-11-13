"""Shared fixtures: a tiny throw-away project (config + limits) in a temp dir."""
from __future__ import annotations

from pathlib import Path

import pytest

from relay.config import load_config

RELAY_YAML = """\
project: {name: relay-test, schema_version: 1}
paths:
  papers_dir: data/papers
  processed_dir: data/processed
  manifest: data/manifest.json
  limits: config/limits.yaml
  results_dir: results
  questions: data/questions.yaml
papers:
  - {key: attention, arxiv_id: "1706.03762", version: 7, title: "Attention Is All You Need"}
models:
  primary: test/model
  registry:
    test/model:
      provider: groq
      base_url: https://example.invalid/v1
      limits_key: test/model
      price_in_per_mtok: 0.1
      price_out_per_mtok: 0.2
      price_provenance: DOCUMENTED
retrieval:
  default_setting: S1
  abstain_min_coverage: 0.5
  mmr_lambda: 0.7
  settings:
    S1: {chunk_tokens: 100, overlap_tokens: 10, k: 2, fetch_k: 4, rerank: none, section_aware: false}
"""

LIMITS_YAML = """\
account_limits:
  models:
    test/model: {rpm: 30, rpd: 1000, tpm: 8000, tpd: 200000}
"""


def write_project(root: Path) -> Path:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "relay.yaml").write_text(RELAY_YAML, encoding="utf-8")
    (root / "config" / "limits.yaml").write_text(LIMITS_YAML, encoding="utf-8")
    return root


@pytest.fixture
def standin_url():
    """A live stand-in server on a free local port, for tests that need real streaming."""
    import socket
    import threading
    import time

    import uvicorn

    from relay.standin.profile import StandinProfile
    from relay.standin.server import create_app

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(create_app(StandinProfile(p_hang=0.0, p_5xx=0.0, p_midstream=0.0, p_slow=0.0, p_bad_citation=0.0)), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    t0 = time.time()
    while not server.started:
        if time.time() - t0 > 10:
            raise RuntimeError("stand-in test server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return write_project(tmp_path)


@pytest.fixture
def cfg(project: Path):
    return load_config(project)
