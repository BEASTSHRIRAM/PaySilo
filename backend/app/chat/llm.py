"""Provider-agnostic LLM layer for Silo chat.

``LLM_PROVIDER`` selects the provider: ``anthropic`` (default), ``openai``,
``groq`` or ``meta``. Each provider has its own key/model environment:

- anthropic: ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_MODEL``
- openai:    ``OPENAI_API_KEY`` / ``OPENAI_MODEL``
- groq:      ``GROQ_API_KEY`` / ``GROQ_MODEL``
- meta:      ``META_API_KEY`` / ``META_MODEL`` + ``META_BASE_URL``

OpenAI, Groq and Meta are all reached through an OpenAI-compatible chat
model (``langchain-openai`` with a ``base_url``); Anthropic uses
``langchain-anthropic``. When the selected provider has **no key
configured**, :func:`get_chat_model` returns ``None`` and the chat service
uses its deterministic fallback responder over the same tools — it never
crashes and never pretends to be an LLM.
"""

from __future__ import annotations

import logging
import os

from ..config import Settings

logger = logging.getLogger(__name__)

PROVIDERS = ("anthropic", "openai", "groq", "meta")

# OpenAI-compatible endpoints per provider (Meta's is configurable).
_OPENAI_COMPATIBLE_BASE_URLS = {
    "openai": None,  # langchain-openai default endpoint
    "groq": "https://api.groq.com/openai/v1",
}


def provider_config(settings: Settings) -> tuple[str, str | None, str, str | None]:
    """(provider, api_key, model, base_url) for the configured provider.

    Settings win; raw environment variables are honoured as a fallback so
    the layer also works when constructed outside the app factory.
    """
    provider = (settings.llm_provider or os.environ.get("LLM_PROVIDER") or "anthropic").lower()
    if provider not in PROVIDERS:
        logger.warning("Unknown LLM_PROVIDER %r; falling back to anthropic", provider)
        provider = "anthropic"
    if provider == "anthropic":
        return provider, settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"), \
            settings.anthropic_model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"), None
    if provider == "openai":
        return provider, settings.openai_api_key or os.environ.get("OPENAI_API_KEY"), \
            settings.openai_model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini"), \
            _OPENAI_COMPATIBLE_BASE_URLS["openai"]
    if provider == "groq":
        return provider, settings.groq_api_key or os.environ.get("GROQ_API_KEY"), \
            settings.groq_model or os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"), \
            _OPENAI_COMPATIBLE_BASE_URLS["groq"]
    return provider, settings.meta_api_key or os.environ.get("META_API_KEY"), \
        settings.meta_model or os.environ.get("META_MODEL", "llama-3.3-70b-instruct"), \
        settings.meta_base_url or os.environ.get("META_BASE_URL", "https://api.llama.com/v1")


def get_chat_model(settings: Settings):
    """Return a LangChain chat model for the configured provider, or None.

    ``None`` means "no key for the selected provider" — the caller must
    use the deterministic fallback responder.
    """
    provider, api_key, model, base_url = provider_config(settings)
    if not api_key:
        logger.info("Silo chat: no API key for provider %r; deterministic fallback responder active", provider)
        return None
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model, api_key=api_key, temperature=0, max_tokens=2048)
    from langchain_openai import ChatOpenAI

    kwargs: dict = {"model": model, "api_key": api_key, "temperature": 0, "max_tokens": 2048}
    if base_url:
        kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs)
