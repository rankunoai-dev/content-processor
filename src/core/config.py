"""Typed application configuration loaded from the environment.

Rules enforced here:

* No module anywhere else may read `os.environ` directly. Everything goes
  through `get_settings()` so that configuration is typed, validated once, and
  greppable.
* Secrets are held as `SecretStr` so they cannot be accidentally printed or
  serialised into an audit log.
* Production refuses to boot with guardrails disabled.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.errors import ConfigurationError

__all__ = ["Environment", "Settings", "get_settings", "reset_settings_cache"]

REPO_ROOT = Path(__file__).resolve().parents[2]


class Environment(StrEnum):
    """Deployment target. Governs how strict the guardrails are."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """All runtime configuration for the platform.

    Field names map to upper-cased environment variables (see `.env.example`).
    """

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- Application -------------------------------------------------------
    environment: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"
    log_format: str = Field(default="json", pattern="^(json|text)$")
    audit_log_path: Path = REPO_ROOT / "logs" / "audit.jsonl"

    # -- Guardrails --------------------------------------------------------
    guardrails_enabled: bool = Field(
        default=True,
        description="Master switch. Disabling is permitted in development only.",
    )
    require_approval_for_writes: bool = True
    require_approval_for_spend: bool = True
    max_session_spend_usd: float = Field(
        default=5.0,
        ge=0.0,
        description="Hard ceiling on cumulative spend for one process.",
    )

    # -- Rate limiting -----------------------------------------------------
    default_requests_per_minute: int = Field(default=60, gt=0)
    default_max_retries: int = Field(default=3, ge=0, le=10)
    default_timeout_s: float = Field(default=30.0, gt=0.0)

    # -- LLM providers -----------------------------------------------------
    gemini_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None

    # -- Google Search Console --------------------------------------------
    google_search_console_client_email: str | None = None
    google_search_console_private_key: SecretStr | None = None

    # -- Google Ads --------------------------------------------------------
    google_ads_developer_token: SecretStr | None = None
    google_ads_client_id: str | None = None
    google_ads_client_secret: SecretStr | None = None
    google_ads_refresh_token: SecretStr | None = None

    # -- SEO data providers ------------------------------------------------
    serp_api_key: SecretStr | None = None
    ahrefs_api_key: SecretStr | None = None
    semrush_api_key: SecretStr | None = None

    # -- Content processor: ledger and evidence ----------------------------
    cp_database_url: SecretStr | None = Field(
        default=None, description="Postgres DSN for the task ledger (contains a password)."
    )
    cp_evidence_root: Path = REPO_ROOT / "evidence"
    # Latest capture of every URL from every run (recrawl/history.py). Keep it next to
    # the evidence root: reused content cites evidence stored there.
    cp_url_history_root: Path = REPO_ROOT / "url_history"
    # App module 1: projects, their URL lists, runs and reports (never committed).
    cp_app_data_root: Path = REPO_ROOT / "app_data"
    # Documents and images read at once during a scrape (local work, no requests to a
    # site). 4 suits a workstation; set 1 or 2 in a container with 1-2 GB of memory.
    cp_doc_parallel_tasks: int = Field(default=4, ge=1, le=16)

    # -- Content processor: knowledge base (RFC 0002 stages 5-7) -----------
    # Embedded vector store, used when Postgres with pgvector is not configured.
    cp_knowledge_root: Path = REPO_ROOT / "knowledge_store"
    # CPU fallback embedder (fastembed, the `fallback` extra) used when Ollama is not
    # available; "" switches it off (then the hashing stand-in is used). Downloaded once.
    cp_fallback_embed_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    cp_model_cache_dir: Path = REPO_ROOT / "model_cache"
    # OpenAI-compatible model endpoint (Ollama). Accepted: this machine, a private address,
    # or a host name that resolves only to private addresses (an in-house server). A public
    # (cloud) host needs CP_ALLOW_REMOTE_MODEL_ENDPOINT=true and https; page text is sent to it.
    cp_model_endpoint: str = "http://localhost:11434/v1"
    cp_allow_remote_model_endpoint: bool = False
    cp_model_api_key: SecretStr | None = Field(
        default=None, description="Bearer token for a model server behind an authenticating proxy"
    )
    cp_embed_model: str = "bge-m3"
    cp_chat_model: str = "qwen2.5:7b-instruct"

    # -- Analyst action cards (Claude, src/modules/analyst) ----------------
    # Off until the product owner confirms what data may go to Claude. Caps work like
    # the paid-scraping caps: a monthly cap of 0 means unset, and runs are refused.
    cp_claude_enabled: bool = False
    cp_claude_model: str = "claude-opus-5-5"
    cp_claude_effort: str = "high"
    cp_claude_run_cap_usd: float = Field(default=2.0, ge=0.0)
    cp_claude_daily_cap_usd: float = Field(default=10.0, ge=0.0)
    cp_claude_monthly_cap_usd: float = Field(
        default=0.0, ge=0.0, description="0 means unset: Claude runs refuse to start until set."
    )
    cp_claude_max_tool_calls: int = Field(default=40, ge=1, le=200)

    # -- Research module (Semrush, src/modules/research) --------------------
    # Off until switched on; a monthly unit cap of 0 means unset and refuses every pull.
    # Costs are units per returned line; check them against your Semrush plan.
    cp_semrush_enabled: bool = False
    cp_semrush_monthly_units: int = Field(default=0, ge=0)
    cp_semrush_pull_units: int = Field(default=5000, ge=0, description="Most one pull may use")
    cp_semrush_database: str = "us"
    cp_semrush_keyword_limit: int = Field(default=200, ge=1, le=10000)
    cp_semrush_min_interval_ms: int = Field(
        default=500, ge=0, le=10000, description="Pause between two Semrush calls (all pulls)"
    )
    cp_semrush_fresh_days: int = Field(
        default=14, ge=0, le=365, description="Reuse a website's pull younger than this; 0 = never"
    )

    # -- Content Scraper: the shared scrape queue and its emails ------------
    # One scrape runs at a time on the instance; the others wait in one queue.
    # Without an SMTP host, emails are written to <app data>/outbox/ instead of sent.
    cp_smtp_host: str | None = None
    cp_smtp_port: int = Field(default=587, ge=1, le=65535)
    cp_smtp_user: str | None = None
    cp_smtp_password: SecretStr | None = None
    cp_smtp_from: str | None = Field(default=None, description="sender address of queue emails")
    cp_smtp_starttls: bool = True
    cp_app_base_url: str = Field(
        default="http://127.0.0.1:8780", description="where the app is opened, for email links"
    )
    cp_queue_ready_hours: float = Field(
        default=72.0,
        gt=0.0,
        description="how long a scrape whose turn came waits to be started before it is cancelled",
    )

    # -- Content processor: Bright Data (T4 proxies / T5 unlocker) ---------
    # Caps are enforced in code before any request leaves (playbook §10, layer 2).
    brightdata_enabled: bool = Field(default=False, description="Kill switch for T4/T5.")
    brightdata_customer_id: str | None = None
    brightdata_proxy_zone: str | None = None
    brightdata_proxy_zone_password: SecretStr | None = None
    brightdata_unlocker_zone: str | None = None
    brightdata_unlocker_zone_password: SecretStr | None = None
    brightdata_run_cap_usd: float = Field(default=5.0, ge=0.0)
    brightdata_daily_cap_usd: float = Field(default=25.0, ge=0.0)
    brightdata_monthly_cap_usd: float = Field(
        default=0.0, ge=0.0, description="0 means unset: T4/T5 refuse to start until set."
    )
    brightdata_url_cap_usd: float = Field(
        default=0.25, ge=0.0, description="Most one URL may cost across all its paid attempts."
    )
    brightdata_proxy_host: str = "brd.superproxy.io"
    brightdata_proxy_port: int = Field(default=33335, ge=1, le=65535)
    brightdata_ca_cert_path: Path | None = Field(
        default=None,
        description="Bright Data's CA certificate; the Web Unlocker (T5) needs it. "
        "TLS verification is never switched off.",
    )
    # Prices are deliberately on the high side so a cap is reached early, not late.
    brightdata_proxy_usd_per_gb: float = Field(default=10.0, gt=0.0)
    brightdata_unlocker_usd_per_1k: float = Field(default=3.0, gt=0.0)
    brightdata_paid_max_bytes: int = Field(
        default=5 * 1024 * 1024, ge=1, description="Body cap for a paid fetch (bounds its cost)."
    )
    cp_spend_dir: Path = Field(
        default=REPO_ROOT / "spend",
        description="Append-only spend and approval logs for paid tiers.",
    )

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        """Normalise and validate the log level."""
        normalised = value.upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if normalised not in allowed:
            msg = f"LOG_LEVEL must be one of {sorted(allowed)}, got '{value}'"
            raise ValueError(msg)
        return normalised

    def model_post_init(self, _context: Any, /) -> None:
        """Refuse unsafe production configurations at boot rather than at call time."""
        if self.environment is Environment.PRODUCTION and not self.guardrails_enabled:
            msg = "GUARDRAILS_ENABLED=false is not permitted in production."
            raise ConfigurationError(msg)

    def require(self, field_name: str) -> str:
        """Return a required credential, or fail loudly with an actionable message.

        Args:
            field_name: Name of a settings field holding a credential.

        Returns:
            The plain-text value.

        Raises:
            ConfigurationError: If the field is unset or is not a known field.
        """
        if field_name not in type(self).model_fields:
            msg = f"'{field_name}' is not a known setting."
            raise ConfigurationError(msg)

        value = getattr(self, field_name)
        if value is None:
            msg = (
                f"Required setting '{field_name.upper()}' is not configured. "
                f"Add it to your .env file (see .env.example)."
            )
            raise ConfigurationError(msg)
        return value.get_secret_value() if isinstance(value, SecretStr) else str(value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that `.env` is parsed exactly once and every module observes an
    identical view of configuration.
    """
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache. Intended for tests only."""
    get_settings.cache_clear()
