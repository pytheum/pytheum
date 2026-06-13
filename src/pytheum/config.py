"""Serve-side environment configuration for the Pytheum API.

All 16 environment variables that are boundary-classified **S** (pytheum-serve)
in the 2026-06-13 Stage-0 reference are captured here as a single
:class:`ServeConfig` model backed by pydantic-settings.

Fields map 1-to-1 to environment variable names via ``validation_alias``.
Defaults mirror the values documented in the stage-0 reference; variables
without a default are typed ``str | None`` and default to ``None``.

Usage::

    from pytheum.config import ServeConfig

    cfg = ServeConfig()          # reads from os.environ
    print(cfg.stream_port)       # int, from PYTHEUM_STREAM_PORT

Or for testing::

    cfg = ServeConfig(stream_port=9999)  # direct construction bypasses env
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServeConfig(BaseSettings):
    """Pydantic-settings model for all serve-side environment variables.

    Construction priority (highest first):
    1. Direct keyword arguments (useful in tests).
    2. Environment variables (exact names listed on each field).
    3. Defaults declared on each field.
    """

    model_config = SettingsConfigDict(
        populate_by_name=True,
        case_sensitive=False,
        env_ignore_empty=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ #
    # Context-batch limits
    # ------------------------------------------------------------------ #

    context_batch_max: int = Field(
        default=25,
        validation_alias="PYTHEUM_CONTEXT_BATCH_MAX",
        description=(
            "Maximum number of markets per context-batch call "
            "(PYTHEUM_CONTEXT_BATCH_MAX)."
        ),
    )
    context_batch_concurrency: int = Field(
        default=5,
        validation_alias="PYTHEUM_CONTEXT_BATCH_CONCURRENCY",
        description=(
            "Concurrent handler tasks inside a single context-batch request "
            "(PYTHEUM_CONTEXT_BATCH_CONCURRENCY)."
        ),
    )

    # ------------------------------------------------------------------ #
    # CLI / token store
    # ------------------------------------------------------------------ #

    api_token: str | None = Field(
        default=None,
        validation_alias="PYTHEUM_API_TOKEN",
        description=(
            "Bearer token for CLI commands.  Optional — the CLI reads this "
            "from the environment rather than requiring it at server startup "
            "(PYTHEUM_API_TOKEN)."
        ),
    )
    stream_server: str = Field(
        default="wss://api.pytheum.com/v1/stream",
        validation_alias="PYTHEUM_STREAM_SERVER",
        description=(
            "WebSocket server URL used by the CLI stream subcommand "
            "(PYTHEUM_STREAM_SERVER)."
        ),
    )
    stream_tokens_file: str = Field(
        default="/var/lib/pytheum-stream/tokens.jsonl",
        validation_alias="PYTHEUM_STREAM_TOKENS_FILE",
        description=(
            "Path to the JSONL file that stores issued auth tokens "
            "(PYTHEUM_STREAM_TOKENS_FILE)."
        ),
    )

    # ------------------------------------------------------------------ #
    # Data paths
    # ------------------------------------------------------------------ #

    equivalence_path: str | None = Field(
        default=None,
        validation_alias="PYTHEUM_EQUIVALENCE_PATH",
        description=(
            "Explicit path to the equivalence-export artifact.  When absent "
            "the equivalence index falls back to a glob over the data/ "
            "directory (PYTHEUM_EQUIVALENCE_PATH)."
        ),
    )
    related_path: str | None = Field(
        default=None,
        validation_alias="PYTHEUM_RELATED_PATH",
        description=(
            "Explicit path to the related-export artifact.  When absent "
            "the related index falls back to a glob over the data/ directory "
            "(PYTHEUM_RELATED_PATH)."
        ),
    )
    market_categories_path: str = Field(
        default="data/market_categories.json",
        validation_alias="PYTHEUM_MARKET_CATEGORIES_PATH",
        description=(
            "Path to the market-categories JSON file used by screen / params "
            "helpers (PYTHEUM_MARKET_CATEGORIES_PATH).  Migrates to serve in "
            "Stage 1 of the restructure."
        ),
    )

    # ------------------------------------------------------------------ #
    # MCP connector
    # ------------------------------------------------------------------ #

    mcp_rl_per_min: int = Field(
        default=60,
        validation_alias="PYTHEUM_MCP_RL_PER_MIN",
        description=(
            "Sustained request rate limit for the MCP HTTP endpoint "
            "(requests per minute) (PYTHEUM_MCP_RL_PER_MIN)."
        ),
    )
    mcp_rl_burst: int = Field(
        default=60,
        validation_alias="PYTHEUM_MCP_RL_BURST",
        description=(
            "Burst allowance for the MCP HTTP endpoint "
            "(PYTHEUM_MCP_RL_BURST)."
        ),
    )
    mcp_http_port: int = Field(
        default=8444,
        validation_alias="PYTHEUM_MCP_HTTP_PORT",
        description=(
            "TCP port the MCP server listens on (PYTHEUM_MCP_HTTP_PORT)."
        ),
    )
    api_base: str = Field(
        default="https://api.pytheum.com",
        validation_alias="PYTHEUM_API_BASE",
        description=(
            "Base URL the MCP client uses to reach the REST API "
            "(PYTHEUM_API_BASE)."
        ),
    )

    # ------------------------------------------------------------------ #
    # HTTP server
    # ------------------------------------------------------------------ #

    http_port: int = Field(
        default=0,
        validation_alias="PYTHEUM_HTTP_PORT",
        description=(
            "TCP port for the embedded uvicorn HTTP server.  "
            "0 = disabled (PYTHEUM_HTTP_PORT)."
        ),
    )
    stream_log_level: str = Field(
        default="INFO",
        validation_alias="PYTHEUM_STREAM_LOG_LEVEL",
        description=(
            "Python logging level for the server process "
            "(PYTHEUM_STREAM_LOG_LEVEL)."
        ),
    )
    stream_host: str = Field(
        default="127.0.0.1",
        validation_alias="PYTHEUM_STREAM_HOST",
        description=(
            "Bind address for both the WebSocket shim and the embedded "
            "HTTP server (PYTHEUM_STREAM_HOST)."
        ),
    )
    stream_port: int = Field(
        default=8443,
        validation_alias="PYTHEUM_STREAM_PORT",
        description=(
            "TCP port the WebSocket / websockets server listens on "
            "(PYTHEUM_STREAM_PORT)."
        ),
    )
