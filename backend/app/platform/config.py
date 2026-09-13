"""Application configuration.

Settings come from the environment only. Required secrets have no defaults so the
process fails loudly at startup rather than running with an insecure fallback.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LABELLENS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = "local"
    debug: bool = False

    # --- required secrets -------------------------------------------------
    secret_key: str = Field(min_length=32)
    database_url: str
    redis_url: str = "redis://localhost:6379/0"

    # --- sessions ---------------------------------------------------------
    session_cookie_name: str = "ll_session"
    session_idle_seconds: int = 2 * 60 * 60
    session_absolute_seconds: int = 12 * 60 * 60
    cookie_secure: bool = True

    # --- auth hardening ---------------------------------------------------
    login_max_attempts: int = 10
    login_lockout_seconds: int = 15 * 60
    argon2_memory_kib: int = 65536
    argon2_time_cost: int = 3
    argon2_parallelism: int = 2

    # --- api --------------------------------------------------------------
    cors_origins: list[str] = ["http://localhost:5173"]
    max_request_bytes: int = 2 * 1024 * 1024

    # --- object storage (S3-compatible: MinIO locally, R2/B2 in production) ----
    storage_endpoint_url: str = ""
    storage_bucket: str = "labellens-uploads"
    storage_region: str = "us-east-1"
    storage_access_key: str = ""
    storage_secret_key: str = ""
    storage_force_path_style: bool = True
    storage_upload_ttl_seconds: int = 300
    storage_download_ttl_seconds: int = 300
    storage_max_upload_bytes: int = 25 * 1024 * 1024

    # --- ingestion (upload completion, validation, AV) ---------------------
    ingestion_max_pdf_pages: int = 30
    # Empty host means no AV daemon is configured; files are marked `skipped`,
    # never silently "clean" - see app/ingestion/av.py.
    clamd_host: str = ""
    clamd_port: int = 3310

    # --- OCR fallback escalation (P3-T3; D-03 - vendor - still open) -------
    # Only "null" (disabled) exists today: D-03 (Google Vision vs Azure Read)
    # is still an open decision, and either vendor needs real cloud
    # credentials this environment doesn't have - see
    # `app/vision/ocr/escalation.py`'s own module docstring. The escalation
    # *policy* (confidence threshold, per-org daily budget, dual-recording,
    # selection between two real results) is fully real and tested against a
    # fake fallback engine; only the vendor call itself is blocked, exactly
    # the same "config, not code" posture D-06 already established for the
    # LLM provider and D-02 for object storage - adding a real vendor here
    # later is one adapter class plus one new `Literal` member, not a
    # pipeline change.
    ocr_fallback_provider: Literal["null"] = "null"
    # Matches the MEDIUM confidence-tier threshold `app.confidence.tiers`
    # already uses elsewhere in this codebase, so "low enough to escalate"
    # and "low enough to force review" agree with each other.
    ocr_fallback_confidence_threshold: float = 0.70
    # IMPLEMENTATION.md's own AI-threats table calls for a "per-org daily
    # token/page budget" against cost-based DoS; this is that budget's OCR-
    # fallback-specific instance.
    ocr_fallback_daily_budget_per_org: int = 50

    # --- LLM extraction (D-06: Gemini, reached through a neutral adapter) ---
    # Deliberately provider-neutral names: the extraction step is an adapter
    # behind `app/extraction/llm/base.py::ExtractionProvider`, so swapping
    # vendors is a config change, not a rename across the codebase - the same
    # posture that keeps `storage_*` vendor-neutral for D-02.
    llm_provider: Literal["gemini", "null"] = "gemini"
    # Empty key means no provider is configured; extraction fails explicitly
    # rather than silently returning empty facts - see app/extraction/llm/.
    llm_api_key: str = ""
    # Pinned deliberately rather than using a floating alias like
    # `gemini-flash-latest`: this is a compliance product whose whole
    # reproducibility story (model manifests, pinned rulesets, byte-identical
    # re-evaluation) depends on the same inputs producing the same facts a
    # year later. A self-updating model would silently break that.
    llm_model: str = "gemini-3.8-flash"
    # Used only after a schema-validation repair retry has already failed
    # (IMPLEMENTATION.md §8 step 6: "on second failure, escalate model tier").
    # Pointed at the *same* Flash model on purpose: verified 2026-09-02 that
    # every Pro-tier model returns "exceeded your current quota" without
    # billing enabled, so escalating there would guarantee a failed third
    # attempt where a plain retry may still succeed. With billing enabled,
    # set this to a Pro model to restore genuine tier escalation.
    llm_escalation_model: str = "gemini-3.8-flash"
    llm_timeout_seconds: int = 90
    llm_max_output_tokens: int = 8192
    # Extraction must be as close to deterministic as the provider allows:
    # the same label should not yield different facts run to run.
    llm_temperature: float = 0.0

    # --- observability (P7-T5) ---------------------------------------------
    # Empty means Sentry is disabled - local/test runs never send anything
    # anywhere by default, matching every other optional external dependency
    # in this codebase (clamd_host, storage_endpoint_url, ...).
    sentry_dsn: str = ""

    @field_validator("secret_key")
    @classmethod
    def _reject_placeholder_secret(cls, value: str) -> str:
        if value.lower() in {"changeme", "secret", "insecure"}:
            raise ValueError("LABELLENS_SECRET_KEY must not be a placeholder value")
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
