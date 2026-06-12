# config.py
# All configuration via environment variables — 12-factor app pattern.
# See DECISIONS.md D-012.
# pydantic-settings reads env vars with type validation and defaults.

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # URL of the upstream service Aegis proxies to
    upstream_url: str = "http://localhost:9000"

    # SQLite file path. Use ":memory:" in tests.
    db_path: str = "aegis.db"

    # How long an idempotency key lives before expiry (seconds).
    # Default: 24 hours — mirrors Stripe's idempotency window.
    ttl_seconds: int = 86400

    # Port Aegis listens on
    port: int = 8000

    # How often the background eviction loop sweeps expired rows (seconds).
    eviction_interval_seconds: int = 300

    # Timeout for upstream HTTP calls (seconds).
    # Hardcoding 30s in the code would contradict the 12-factor config story.
    upstream_timeout_seconds: int = 30

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )


settings = Settings()