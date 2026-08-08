# config.py
# All configuration via environment variables — 12-factor app pattern.
# See DECISIONS.md D-012.
# pydantic-settings reads env vars with type validation and defaults.
#
# Rule: every operational number in Aegis lives here. A duration hardcoded
# in a module is a value an operator cannot change without a code deploy,
# which is exactly what the 12-factor config story rules out.

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
    upstream_timeout_seconds: int = 30

    # Age at which a startup sweep treats a surviving in_flight row as a crash
    # orphan rather than a live request (seconds). Must comfortably exceed
    # upstream_timeout_seconds — anything younger than this could still be a
    # legitimate in-progress call from a process that has not actually died.
    in_flight_recovery_seconds: int = 60

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )


settings = Settings()
