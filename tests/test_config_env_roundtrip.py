"""Unit tests for pytheum.config.ServeConfig — env var round-trip."""
from __future__ import annotations

import pytest

from pytheum.config import ServeConfig


def test_defaults_are_applied_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure clean environment produces documented defaults."""
    # Unset all vars that ServeConfig reads so the test is env-independent.
    vars_to_clear = [
        "PYTHEUM_CONTEXT_BATCH_MAX",
        "PYTHEUM_CONTEXT_BATCH_CONCURRENCY",
        "PYTHEUM_API_TOKEN",
        "PYTHEUM_STREAM_SERVER",
        "PYTHEUM_STREAM_TOKENS_FILE",
        "PYTHEUM_EQUIVALENCE_PATH",
        "PYTHEUM_RELATED_PATH",
        "PYTHEUM_MARKET_CATEGORIES_PATH",
        "PYTHEUM_MCP_RL_PER_MIN",
        "PYTHEUM_MCP_RL_BURST",
        "PYTHEUM_MCP_HTTP_PORT",
        "PYTHEUM_API_BASE",
        "PYTHEUM_HTTP_PORT",
        "PYTHEUM_STREAM_LOG_LEVEL",
        "PYTHEUM_STREAM_HOST",
        "PYTHEUM_STREAM_PORT",
    ]
    for v in vars_to_clear:
        monkeypatch.delenv(v, raising=False)

    cfg = ServeConfig()
    assert cfg.context_batch_max == 25
    assert cfg.context_batch_concurrency == 5
    assert cfg.api_token is None
    assert cfg.stream_server == "wss://api.pytheum.com/v1/stream"
    assert cfg.stream_tokens_file == "/var/lib/pytheum-stream/tokens.jsonl"
    assert cfg.equivalence_path is None
    assert cfg.related_path is None
    assert cfg.market_categories_path == "data/market_categories.json"
    assert cfg.mcp_rl_per_min == 60
    assert cfg.mcp_rl_burst == 60
    assert cfg.mcp_http_port == 8444
    assert cfg.api_base == "https://api.pytheum.com"
    assert cfg.http_port == 0
    assert cfg.stream_log_level == "INFO"
    assert cfg.stream_host == "127.0.0.1"
    assert cfg.stream_port == 8443


def test_env_vars_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env vars must propagate into config fields."""
    monkeypatch.setenv("PYTHEUM_STREAM_PORT", "9999")
    monkeypatch.setenv("PYTHEUM_HTTP_PORT", "8080")
    monkeypatch.setenv("PYTHEUM_STREAM_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("PYTHEUM_MCP_HTTP_PORT", "9444")
    monkeypatch.setenv("PYTHEUM_CONTEXT_BATCH_MAX", "50")
    monkeypatch.setenv("PYTHEUM_API_BASE", "https://staging.pytheum.com")
    monkeypatch.setenv("PYTHEUM_STREAM_HOST", "0.0.0.0")

    cfg = ServeConfig()

    assert cfg.stream_port == 9999
    assert cfg.http_port == 8080
    assert cfg.stream_log_level == "DEBUG"
    assert cfg.mcp_http_port == 9444
    assert cfg.context_batch_max == 50
    assert cfg.api_base == "https://staging.pytheum.com"
    assert cfg.stream_host == "0.0.0.0"


def test_optional_path_vars_accept_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHEUM_EQUIVALENCE_PATH", "/data/eq.jsonl.gz")
    monkeypatch.setenv("PYTHEUM_RELATED_PATH", "/data/rel.jsonl.gz")
    monkeypatch.setenv("PYTHEUM_API_TOKEN", "tok_test_abc123")

    cfg = ServeConfig()

    assert cfg.equivalence_path == "/data/eq.jsonl.gz"
    assert cfg.related_path == "/data/rel.jsonl.gz"
    assert cfg.api_token == "tok_test_abc123"


def test_direct_construction_bypasses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct keyword construction is useful in tests and must override env."""
    monkeypatch.setenv("PYTHEUM_STREAM_PORT", "9999")
    cfg = ServeConfig(stream_port=1234)
    assert cfg.stream_port == 1234
