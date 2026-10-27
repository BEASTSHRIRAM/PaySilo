"""Environment configuration. No secrets live in code; Sriram adds real
keys to ``.env`` (see ``.env.example``) when going live.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    app_env: str = os.environ.get("APP_ENV", "demo")
    airwallex_client_id: str | None = os.environ.get("AIRWALLEX_CLIENT_ID")
    airwallex_api_key: str | None = os.environ.get("AIRWALLEX_API_KEY")
    airwallex_base_url: str = os.environ.get(
        "AIRWALLEX_BASE_URL", "https://api-demo.airwallex.com/api/v1"
    )
    anthropic_api_key: str | None = os.environ.get("ANTHROPIC_API_KEY")
    database_url: str | None = os.environ.get("DATABASE_URL")
    metal_rpc_url: str | None = os.environ.get("METAL_RPC_URL")
    metal_private_key: str | None = os.environ.get("METAL_PRIVATE_KEY")
    metal_registry_address: str | None = os.environ.get("METAL_REGISTRY_ADDRESS")

    @property
    def demo_mode(self) -> bool:
        """Demo mode = explicitly requested, or no Airwallex keys present."""
        return self.app_env == "demo" or not (self.airwallex_client_id and self.airwallex_api_key)


def load_settings() -> Settings:
    return Settings(
        app_env=os.environ.get("APP_ENV", "demo"),
        airwallex_client_id=os.environ.get("AIRWALLEX_CLIENT_ID"),
        airwallex_api_key=os.environ.get("AIRWALLEX_API_KEY"),
        airwallex_base_url=os.environ.get(
            "AIRWALLEX_BASE_URL", "https://api-demo.airwallex.com/api/v1"
        ),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        database_url=os.environ.get("DATABASE_URL"),
        metal_rpc_url=os.environ.get("METAL_RPC_URL"),
        metal_private_key=os.environ.get("METAL_PRIVATE_KEY"),
        metal_registry_address=os.environ.get("METAL_REGISTRY_ADDRESS"),
    )
