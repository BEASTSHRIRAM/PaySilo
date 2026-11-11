"""Environment configuration.

Values come from real environment variables first, then from a ``.env``
file (repo root or ``backend/``). No secrets live in code — see
``.env.example`` for every key. Set ``PAYSILO_ENV_FILE=`` (empty) to skip
``.env`` loading entirely (the test suite does this).
"""

from __future__ import annotations

import os
import secrets
from decimal import Decimal
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
    airwallex_base_url: str = "https://api.sandbox.airwallex.com/api/v1"
    # Back-off knobs: 429 retries, and how long to keep retrying
    # ``unconfigured_client_fee`` on a freshly activated account.
    airwallex_rate_limit_retries: int = 5
    airwallex_fee_config_retry_seconds: int = 180
    # Hosted onboarding (live mode only): template id issued by Airwallex for
    # the platform's hosted onboarding flow, and where the employer returns
    # afterwards. Without a template, sandbox activation proceeds directly
    # through the simulation status endpoint.
    airwallex_hosted_onboarding_template: str | None = None
    airwallex_onboarding_return_url: str | None = None

    # Claude — intake/explanation LLM only
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5-5"

    # LangGraph checkpointer: a file path (SQLite) for durable paused runs.
    # When unset, a Postgres DATABASE_URL gives PostgresSaver; otherwise the
    # in-memory fallback is used and paused runs do not survive a restart.
    checkpoint_path: str | None = None

    # Silo chat LLM (provider-agnostic). The selected provider's key decides
    # whether the real model is used; with no key the chat falls back to a
    # deterministic responder over the same tools.
    llm_provider: Literal["anthropic", "openai", "groq", "meta"] = "anthropic"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    groq_api_key: str | None = None
    groq_model: str = "llama-3.3-70b-versatile"
    meta_api_key: str | None = None
    meta_model: str = "llama-3.3-70b-instruct"
    meta_base_url: str = "https://api.llama.com/v1"

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
    # 1099-NEC watch: US-person contractors whose calendar-year-to-date payments reach this many USD.
    # USD 2,000 applies to payments made after 2025-12-31 (vendor sources) - confirm with your tax advisor.
    tax_1099_threshold_usd: Decimal = Decimal("2000")

    # Visa TAP signing key. TAP_PRIVATE_KEY accepts PEM text or base64
    # (base64-encoded PEM, or a base64 raw 32-byte Ed25519 seed). When set,
    # the same key — and a stable, public-key-derived keyid — is used in
    # every process, so signatures survive restarts. When unset, an
    # ephemeral per-process dev key is generated and logged as such.
    tap_private_key: str | None = None
    tap_key_id: str | None = None  # optional explicit keyid override
    tap_agent_id: str = "paysilo-agent"

    # Metal L2 testnet attestation (fails closed until all three are set)
    metal_rpc_url: str | None = None
    metal_private_key: str | None = None
    metal_registry_address: str | None = None
    metal_explorer_url: str | None = None  # e.g. https://testnet-explorer.example (no trailing slash needed)

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
