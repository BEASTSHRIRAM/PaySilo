"""Environment configuration.

Values come from real environment variables first, then from a ``.env``
file (repo root or ``backend/``). No secrets live in code — see
``.env.example`` for every key. Set ``PAYSILO_ENV_FILE=`` (empty) to skip
``.env`` loading entirely (the test suite does this).
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent


def _env_files() -> tuple[str, ...]:
    override = os.environ.get("PAYSILO_ENV_FILE")
    if override is not None:
        return (override,) if override else ()
    return (str(REPO_DIR / ".env"), str(BACKEND_DIR / ".env"))


class Settings(BaseSettings):
    # env_ignore_empty: ``KEY=`` in .env means "not set", not "empty string".
    model_config = SettingsConfigDict(env_file=_env_files(), env_file_encoding="utf-8",
                                      extra="ignore", env_ignore_empty=True)

    app_env: Literal["demo", "live"] = "demo"

    # Airwallex sandbox
    airwallex_client_id: str | None = None
    airwallex_api_key: str | None = None
    airwallex_base_url: str = "https://api-demo.airwallex.com/api/v1"

    # Claude — intake/explanation LLM only
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5-5"

    # Database: any SQLAlchemy URL. Supabase: paste the Postgres connection
    # string. Unset -> a local SQLite file so the demo needs nothing.
    database_url: str | None = None
    auto_create_schema: bool = True

    # Auth
    jwt_secret: str | None = None
    session_ttl_minutes: int = 60 * 12
    cookie_secure: bool = False
    cors_origins: list[str] = ["http://localhost:3000"]
    seed_demo: bool = True

    # Metal L2 testnet attestation (fails closed until all three are set)
    metal_rpc_url: str | None = None
    metal_private_key: str | None = None
    metal_registry_address: str | None = None

    @property
    def demo_mode(self) -> bool:
        """Demo mode = explicitly requested, or no Airwallex keys present."""
        return self.app_env == "demo" or not (self.airwallex_client_id and self.airwallex_api_key)

    def session_secret(self) -> str:
        if self.jwt_secret:
            return self.jwt_secret
        if self.app_env == "live":
            raise RuntimeError("JWT_SECRET must be set when APP_ENV=live.")
        return _ephemeral_secret()


@lru_cache(maxsize=1)
def _ephemeral_secret() -> str:
    # Demo only: sessions are invalidated on restart. Set JWT_SECRET to keep them.
    return secrets.token_urlsafe(48)


def load_settings(**overrides: object) -> Settings:
    settings = Settings(**overrides)  # type: ignore[arg-type]
    if settings.anthropic_api_key and not os.environ.get("ANTHROPIC_API_KEY"):
        # The intake agent reads the key from the process environment.
        os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
    if settings.anthropic_model and not os.environ.get("ANTHROPIC_MODEL"):
        os.environ["ANTHROPIC_MODEL"] = settings.anthropic_model
    return settings
