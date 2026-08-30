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
