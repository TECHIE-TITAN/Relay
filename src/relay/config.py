"""Configuration loading and validation.

Reads config/relay.yaml and config/limits.yaml into typed, immutable objects.
The API key is read from the environment only, and is never printed or
returned by any `describe()`-style helper.

Model limits come from the `account_limits.models` section of limits.yaml
(provenance DOCUMENTED). Nothing in this module calls any network service.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

from .schema import RelayError

RERANK_MODES = ("none", "mmr")


class ConfigError(RelayError):
    """Raised for any configuration problem. The message says what to fix."""


@dataclass(frozen=True)
class PaperSpec:
    key: str
    arxiv_id: str
    version: int
    title: str

    @property
    def versioned_id(self) -> str:
        return f"{self.arxiv_id}v{self.version}"

    @property
    def pdf_url(self) -> str:
        return f"https://arxiv.org/pdf/{self.versioned_id}"

    @property
    def filename(self) -> str:
        return f"{self.key}_{self.versioned_id}.pdf"


@dataclass(frozen=True)
class ModelLimits:
    rpm: int
    rpd: int
    tpm: int
    tpd: int


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: str
    base_url: str
    limits: ModelLimits
    price_in_per_mtok: Optional[float]
    price_out_per_mtok: Optional[float]
    price_provenance: str


@dataclass(frozen=True)
class RetrievalSetting:
    id: str
    chunk_tokens: int
    overlap_tokens: int
    k: int
    fetch_k: int
    rerank: str
    section_aware: bool

    def __post_init__(self) -> None:
        if self.chunk_tokens <= 0:
            raise ConfigError(f"setting {self.id}: chunk_tokens must be positive")
        if not 0 <= self.overlap_tokens < self.chunk_tokens:
            raise ConfigError(f"setting {self.id}: overlap_tokens must be in [0, chunk_tokens)")
        if self.k <= 0 or self.fetch_k < self.k:
            raise ConfigError(f"setting {self.id}: need 0 < k <= fetch_k")
        if self.rerank not in RERANK_MODES:
            raise ConfigError(f"setting {self.id}: rerank must be one of {RERANK_MODES}")


@dataclass(frozen=True)
class Paths:
    root: Path
    papers_dir: Path
    processed_dir: Path
    manifest: Path
    limits: Path
    results_dir: Path
    questions: Path

    @property
    def pages_file(self) -> Path:
        return self.processed_dir / "pages.jsonl"

    @property
    def extract_report(self) -> Path:
        return self.processed_dir / "extract_report.json"

    def chunks_file(self, setting_id: str) -> Path:
        return self.processed_dir / f"chunks_{setting_id}.jsonl"

    @property
    def retrieval_eval_dir(self) -> Path:
        return self.results_dir / "retrieval_eval"


@dataclass(frozen=True)
class Config:
    paths: Paths
    papers: tuple[PaperSpec, ...]
    primary_model: str
    models: dict[str, ModelSpec]
    settings: dict[str, RetrievalSetting]
    default_setting: str
    abstain_min_coverage: float
    mmr_lambda: float

    @property
    def root(self) -> Path:
        return self.paths.root

    def paper(self, key: str) -> PaperSpec:
        for p in self.papers:
            if p.key == key:
                return p
        raise ConfigError(f"unknown paper '{key}'. Known: {[p.key for p in self.papers]}")

    def setting(self, setting_id: str) -> RetrievalSetting:
        try:
            return self.settings[setting_id]
        except KeyError:
            raise ConfigError(
                f"unknown retrieval setting '{setting_id}'. Known: {sorted(self.settings)}"
            ) from None

    def model(self, model_id: Optional[str] = None) -> ModelSpec:
        mid = model_id or self.primary_model
        try:
            return self.models[mid]
        except KeyError:
            raise ConfigError(f"unknown model '{mid}'. Known: {sorted(self.models)}") from None

    def describe(self) -> dict[str, Any]:
        """A JSON-safe summary. Contains no secrets."""
        m = self.model()
        return {
            "root": str(self.root),
            "primary_model": m.id,
            "primary_model_limits": {
                "rpm": m.limits.rpm, "rpd": m.limits.rpd,
                "tpm": m.limits.tpm, "tpd": m.limits.tpd,
                "provenance": "DOCUMENTED (account console snapshot, see config/limits.yaml)",
            },
            "models": sorted(self.models),
            "papers": [
                {"key": p.key, "id": p.versioned_id, "title": p.title} for p in self.papers
            ],
            "retrieval": {
                "default_setting": self.default_setting,
                "abstain_min_coverage": self.abstain_min_coverage,
                "mmr_lambda": self.mmr_lambda,
                "settings": {
                    s.id: {
                        "chunk_tokens": s.chunk_tokens, "overlap_tokens": s.overlap_tokens,
                        "k": s.k, "fetch_k": s.fetch_k, "rerank": s.rerank,
                        "section_aware": s.section_aware,
                    }
                    for s in self.settings.values()
                },
            },
        }


def find_root(start: Optional[Path] = None) -> Path:
    """Locate the project root (the directory containing config/relay.yaml)."""
    env = os.environ.get("RELAY_ROOT")
    if env:
        p = Path(env).expanduser().resolve()
        if not (p / "config" / "relay.yaml").is_file():
            raise ConfigError(f"RELAY_ROOT={p} does not contain config/relay.yaml")
        return p
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        if (d / "config" / "relay.yaml").is_file():
            return d
    raise ConfigError(
        "Could not find config/relay.yaml. Run from inside the project, or set RELAY_ROOT."
    )


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"missing file: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML ({e})") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return data


def _get(d: Any, key: str, ctx: str, typ: Optional[type] = None) -> Any:
    if not isinstance(d, dict) or key not in d:
        raise ConfigError(f"{ctx}: missing required key '{key}'")
    v = d[key]
    if typ is not None and not isinstance(v, typ):
        raise ConfigError(f"{ctx}.{key}: expected {typ.__name__}, got {type(v).__name__}")
    return v


def load_config(root: Optional[Path | str] = None) -> Config:
    root_path = Path(root).resolve() if root else find_root()
    raw = _read_yaml(root_path / "config" / "relay.yaml")

    p = _get(raw, "paths", "relay.yaml", dict)
    paths = Paths(
        root=root_path,
        papers_dir=root_path / _get(p, "papers_dir", "paths", str),
        processed_dir=root_path / _get(p, "processed_dir", "paths", str),
        manifest=root_path / _get(p, "manifest", "paths", str),
        limits=root_path / _get(p, "limits", "paths", str),
        results_dir=root_path / _get(p, "results_dir", "paths", str),
        questions=root_path / _get(p, "questions", "paths", str),
    )

    papers: list[PaperSpec] = []
    for i, entry in enumerate(_get(raw, "papers", "relay.yaml", list)):
        ctx = f"papers[{i}]"
        papers.append(
            PaperSpec(
                key=str(_get(entry, "key", ctx)),
                arxiv_id=str(_get(entry, "arxiv_id", ctx)),
                version=int(_get(entry, "version", ctx)),
                title=str(_get(entry, "title", ctx)),
            )
        )
    keys = [x.key for x in papers]
    if len(set(keys)) != len(keys):
        raise ConfigError(f"relay.yaml: duplicate paper keys in {keys}")

    # Model limits come from limits.yaml (account_limits.models), DOCUMENTED.
    limits_doc = _read_yaml(paths.limits)
    account = (limits_doc.get("account_limits") or {}).get("models") or {}

    models_raw = _get(raw, "models", "relay.yaml", dict)
    primary = str(_get(models_raw, "primary", "models"))
    registry = _get(models_raw, "registry", "models", dict)
    models: dict[str, ModelSpec] = {}
    for mid, spec in registry.items():
        ctx = f"models.registry.{mid}"
        lim_key = str(_get(spec, "limits_key", ctx))
        if lim_key not in account:
            raise ConfigError(
                f"{ctx}: no limits for '{lim_key}' in {paths.limits.name} "
                f"(account_limits.models). Known: {sorted(account)}"
            )
        lim = account[lim_key]
        try:
            limits = ModelLimits(
                rpm=int(lim["rpm"]), rpd=int(lim["rpd"]), tpm=int(lim["tpm"]), tpd=int(lim["tpd"])
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ConfigError(f"{paths.limits.name}: bad limits entry for '{lim_key}' ({e})") from e
        models[str(mid)] = ModelSpec(
            id=str(mid),
            provider=str(_get(spec, "provider", ctx)),
            base_url=str(_get(spec, "base_url", ctx)),
            limits=limits,
            price_in_per_mtok=spec.get("price_in_per_mtok"),
            price_out_per_mtok=spec.get("price_out_per_mtok"),
            price_provenance=str(spec.get("price_provenance", "not stated")),
        )
    if primary not in models:
        raise ConfigError(f"models.primary '{primary}' is not in models.registry {sorted(models)}")

    r = _get(raw, "retrieval", "relay.yaml", dict)
    settings: dict[str, RetrievalSetting] = {}
    for sid, s in _get(r, "settings", "retrieval", dict).items():
        ctx = f"retrieval.settings.{sid}"
        settings[str(sid)] = RetrievalSetting(
            id=str(sid),
            chunk_tokens=int(_get(s, "chunk_tokens", ctx)),
            overlap_tokens=int(_get(s, "overlap_tokens", ctx)),
            k=int(_get(s, "k", ctx)),
            fetch_k=int(_get(s, "fetch_k", ctx)),
            rerank=str(_get(s, "rerank", ctx)),
            section_aware=bool(_get(s, "section_aware", ctx)),
        )
    default_setting = str(_get(r, "default_setting", "retrieval"))
    if default_setting not in settings:
        raise ConfigError(f"retrieval.default_setting '{default_setting}' not in {sorted(settings)}")

    cov = float(_get(r, "abstain_min_coverage", "retrieval"))
    if not 0.0 <= cov <= 1.0:
        raise ConfigError("retrieval.abstain_min_coverage must be between 0 and 1")
    lam = float(_get(r, "mmr_lambda", "retrieval"))
    if not 0.0 <= lam <= 1.0:
        raise ConfigError("retrieval.mmr_lambda must be between 0 and 1")

    return Config(
        paths=paths,
        papers=tuple(papers),
        primary_model=primary,
        models=models,
        settings=settings,
        default_setting=default_setting,
        abstain_min_coverage=cov,
        mmr_lambda=lam,
    )


def get_api_key(root: Optional[Path] = None) -> Optional[str]:
    """Return GROQ_API_KEY from the environment (loading root/.env if present).

    Returns None if unset or empty. Nothing in Phase 0/1 calls this except
    `cli config`, which only reports whether a key is set.
    """
    if root is not None:
        env_file = Path(root) / ".env"
        if env_file.is_file():
            load_dotenv(env_file, override=False)
    key = os.environ.get("GROQ_API_KEY")
    return key if key else None


def mask_secret(secret: Optional[str]) -> str:
    """Describe a secret without revealing any of its characters."""
    if not secret:
        return "not set"
    return f"set (length {len(secret)})"
