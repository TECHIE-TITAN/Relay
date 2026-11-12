from pathlib import Path

import pytest

from relay.config import ConfigError, get_api_key, load_config, mask_secret

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_real_config_loads_primary_model_limits():
    """Acceptance: the real config returns gpt-oss-20b with the documented account limits."""
    cfg = load_config(REPO_ROOT)
    m = cfg.model()
    assert m.id == "openai/gpt-oss-20b"
    assert (m.limits.rpm, m.limits.rpd, m.limits.tpm, m.limits.tpd) == (30, 1000, 8000, 200000)
    assert [p.versioned_id for p in cfg.papers] == ["1706.03762v7", "2410.00037v1", "2507.07955v2"]
    assert set(cfg.settings) == {"S1", "S2", "S3", "S4"}


def test_unknown_model_and_setting_raise_clear_errors(cfg):
    with pytest.raises(ConfigError, match="unknown model 'nope'"):
        cfg.model("nope")
    with pytest.raises(ConfigError, match="unknown retrieval setting"):
        cfg.setting("S9")
    with pytest.raises(ConfigError, match="unknown paper"):
        cfg.paper("nope")


def test_missing_limits_for_registry_model_is_an_error(project):
    (project / "config" / "limits.yaml").write_text("account_limits:\n  models: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="no limits for 'test/model'"):
        load_config(project)


def test_invalid_setting_is_rejected(project):
    text = (project / "config" / "relay.yaml").read_text(encoding="utf-8")
    bad = text.replace("overlap_tokens: 10", "overlap_tokens: 100")   # overlap >= chunk size
    (project / "config" / "relay.yaml").write_text(bad, encoding="utf-8")
    with pytest.raises(ConfigError, match="overlap_tokens"):
        load_config(project)


def test_describe_never_contains_the_api_key(cfg, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "super-secret-value-123")
    assert "super-secret-value-123" not in str(cfg.describe())


def test_mask_secret_reveals_no_characters():
    assert mask_secret(None) == "not set"
    assert mask_secret("") == "not set"
    masked = mask_secret("abcd1234secret")
    assert "abcd" not in masked and "secret" not in masked and "14" in masked


def test_get_api_key_reads_env_and_treats_empty_as_unset(project, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert get_api_key(project) is None
    monkeypatch.setenv("GROQ_API_KEY", "")
    assert get_api_key(project) is None
    monkeypatch.setenv("GROQ_API_KEY", "k")
    assert get_api_key(project) == "k"
