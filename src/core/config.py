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

import logging
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from src.core.errors import ConfigurationError

__all__ = [
    "RENDER_PAGE_TIME_MARGIN_S",
    "Environment",
    "Settings",
    "get_settings",
    "reset_settings_cache",
]

REPO_ROOT = Path(__file__).resolve().parents[2]


RENDER_PAGE_TIME_MARGIN_S = 30.0
"""Least gap between the browser's navigation timeout and its whole-page cap (seconds)."""


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
    # Competitor lite scan: the most pages scraped from one website (plain downloads, no
    # browser), and the most pages taken from one address section (/blog, /products...).
    # 150 pages is about 3 minutes of polite requests and a few MB; safe in a container.
    cp_lite_scan_max_pages: int = Field(default=150, ge=10, le=1000)
    cp_lite_scan_per_section: int = Field(default=3, ge=1, le=50)
    # The browser scrapes render with: "chrome" is the installed Google Chrome (falls back
    # to Playwright's bundled Chromium when Chrome is absent); "chromium" is the bundled
    # Chromium straight away (containers: no Chrome is installed there).
    cp_browser_channel: str = Field(default="chrome", pattern="^(chrome|chromium|msedge)$")
    # How long the browser waits for a page to arrive (DOMContentLoaded) before the
    # render fails as `render_timeout`. 30 s suits a normal link; a slow one (a 1 MB
    # page at ~30 KB/s takes 50-80 s) needs 120 or more, else every render fails and
    # the website's circuit opens. Read at the start of each batch, so a change in
    # `.env` reaches the next batch of a running scrape.
    cp_render_navigation_timeout_s: float = Field(default=30.0, ge=5.0, le=600.0)
    # Cap on one whole page in the browser: arrival, settling, screenshot and the
    # exploration of its controls. Kept at least 30 s above the navigation timeout
    # (clamped up with a logged note otherwise) so a page that arrives late still gets
    # its capture. A batch's time budget is not drawn on by a sleeping computer; the
    # gap that counts as sleep grows with this cap (scheduler/budget.py).
    cp_render_max_page_time_s: float = Field(default=150.0, ge=30.0, le=1200.0)
    # How long the browser waits for a page to stop changing (late scripts, hydration)
    # before it is read; the wait ends as soon as the page is quiet for a few seconds.
    cp_render_settle_window_s: float = Field(default=12.0, ge=1.0, le=120.0)

    # -- Content processor: the web app server (webapi) --------------------
    # Where the server listens. 127.0.0.1 is this machine only (the default, for a
    # workstation); a container must bind 0.0.0.0 so the platform can reach it.
    # The command-line flags --host and --port take precedence.
    cp_bind_host: str = "127.0.0.1"
    cp_bind_port: int = Field(default=8780, ge=1, le=65535)
    # Host headers the server answers (DNS-rebinding guard; a comma-separated list).
    # Add the public host name when deployed, for example "myapp.up.railway.app"
    # or "*.up.railway.app"; "*" accepts any host (then the platform must front it).
    cp_allowed_hosts: Annotated[list[str], NoDecode] = ["127.0.0.1", "localhost"]
    # Origins a browser page may make changes from (comma-separated). Each entry is a host
    # name ("myapp.up.railway.app", "*.example.com") compared with the Origin header's
    # host, or a full origin ("https://myapp.up.railway.app") compared whole, scheme and
    # port included. Hosts in CP_ALLOWED_HOSTS are always accepted as origins too, so this
    # is needed only when the pages are served from another address than the API.
    cp_allowed_origins: Annotated[list[str], NoDecode] = ["127.0.0.1", "localhost"]
    # Fully parsed runs the analyst API keeps in memory (each holds every page's records
    # and text). Small on purpose: run summaries for the list are cached separately.
    cp_run_cache_entries: int = Field(default=4, ge=1, le=64)
    # How long a sign-in lasts (hours) once team accounts exist (`accounts create-admin`).
    cp_session_hours: float = Field(default=12.0, gt=0, le=24 * 30)
    # A project report (all of the client's websites) is built in its own process; it is
    # stopped after this many seconds.
    cp_project_report_timeout_s: float = Field(default=1800.0, gt=0.0)
    # Semantic similarity in the project report (analysis/semantic.py): pages that say
    # the same thing in different words, scored on their own content with the knowledge
    # base's embedding model. Off = the project report has no semantic workbook. The
    # paragraph cap bounds memory per page; the batch size is texts per embedding call;
    # the pair cap keeps the strongest pairs of a very repetitive website.
    cp_semantic_enabled: bool = True
    cp_semantic_max_paragraphs: int = Field(default=200, ge=10, le=2000)
    cp_semantic_embed_batch: int = Field(default=64, ge=1, le=1024)
    cp_semantic_max_pairs: int = Field(default=50_000, ge=100, le=2_000_000)
    # Retrieval (knowledge/retrieval.py, vector_store.py). The SQLite store keeps each
    # website's chunk vectors in memory between searches, up to this many MB in total
    # (least recently used dropped first; 0 = read them from disk on every search).
    cp_vector_cache_mb: int = Field(default=256, ge=0, le=16_384)
    # Postgres/pgvector HNSW search depth (pgvector's default 40 returns at most 40 rows).
    cp_hnsw_ef_search: int = Field(default=100, ge=10, le=1_000)
    # Reranker: a cross-encoder reads the question with each candidate passage and scores
    # how well it answers; passages below the minimum score are dropped, and an answer
    # with none left says "not found". auto = used only when the model is already in
    # CP_MODEL_CACHE_DIR (never downloaded during a search; about 1.1 GB of memory);
    # on = download when missing; off = no reranker (no relevance check).
    cp_rerank: Literal["auto", "on", "off"] = "auto"
    cp_rerank_model: str = "jinaai/jina-reranker-v2-base-multilingual"
    # Candidates scored per question, and characters of each read. Measured on John
    # Hancock: clipping passages to 600 characters halved the time but lost the answer
    # deeper in the passage (right page first in 25 of 36 questions, not 31) and left no
    # usable threshold; whole passages (2,000) take about 0.3 s each on a busy CPU. The
    # minimum score: every answerable question's best whole passage scored above -0.3;
    # two of four unanswerable ones fell below -0.8.
    cp_rerank_pool: int = Field(default=12, ge=3, le=200)
    cp_rerank_max_chars: int = Field(default=2_000, ge=100, le=4_000)
    cp_rerank_sentences: int = Field(default=15, ge=1, le=100)
    # CPU threads the reranker may use (0 = all cores; set to the container's vCPUs).
    cp_rerank_threads: int = Field(default=0, ge=0, le=64)
    cp_rerank_min_score: float = Field(default=-0.5, ge=-20.0, le=20.0)
    # Diversity (maximal marginal relevance): 1.0 = relevance only; lower values trade
    # some relevance for passages that differ from those already chosen.
    cp_mmr_lambda: float = Field(default=0.75, ge=0.0, le=1.0)
    # Context builder (knowledge/context.py): tokens of text handed over with the hits
    # (the hits plus their surrounding section), about four characters per token. Answers
    # by the local model get the first (a small model's window); search results for Claude
    # Desktop and the app the second (a larger reader, ~8 passages plus their sections).
    cp_context_tokens: int = Field(default=4_000, ge=0, le=100_000)
    cp_search_context_tokens: int = Field(default=8_000, ge=0, le=100_000)
    # Topic drift in monitoring checks (monitoring/meaning.py): each changed page's old
    # and new versions are compared by meaning with the website's knowledge-base model
    # (never the words-only stand-in). A competitor page scoring below the threshold
    # was moved onto another topic and raises an alert. Off = no meaning scores.
    cp_monitor_meaning: bool = True
    cp_monitor_topic_drift: float = Field(default=0.85, gt=0.0, lt=1.0)

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

    @field_validator("cp_allowed_hosts", "cp_allowed_origins", mode="before")
    @classmethod
    def _split_names(cls, value: object, info: ValidationInfo) -> object:
        """Accept a comma-separated string from the environment as well as a list.

        An empty value (a blank line in `.env`) means "not set": the default applies,
        because an empty list would lock everyone out.
        """
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, list) and not value and info.field_name:
            return list(cls.model_fields[info.field_name].default)
        return value

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

    @model_validator(mode="after")
    def _render_page_time_covers_navigation(self) -> Settings:
        """Keep the whole-page cap at least `RENDER_PAGE_TIME_MARGIN_S` above navigation.

        A page that arrives just inside the navigation timeout still needs time to
        settle and be captured; a cap set lower than that would fail every late page
        after it arrived. The cap is raised, not refused, and the change is logged.
        """
        floor = self.cp_render_navigation_timeout_s + RENDER_PAGE_TIME_MARGIN_S
        if self.cp_render_max_page_time_s < floor:
            logging.getLogger(__name__).warning(
                "CP_RENDER_MAX_PAGE_TIME_S=%.0f is under CP_RENDER_NAVIGATION_TIMEOUT_S "
                "(%.0f) + %.0f s; raised to %.0f",
                self.cp_render_max_page_time_s,
                self.cp_render_navigation_timeout_s,
                RENDER_PAGE_TIME_MARGIN_S,
                floor,
            )
            self.cp_render_max_page_time_s = floor
        return self

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
