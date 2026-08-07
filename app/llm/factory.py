"""
app/llm/factory.py — Returns the correct LLM client based on LLM_PROVIDER in .env.

To switch provider:
  1. Set LLM_PROVIDER=claude  (or copilot / openai / ollama) in .env
  2. Set the matching API key / URL
  3. Restart the server — no code changes needed

To add a new provider:
  1. Create app/llm/my_provider_client.py  (subclass BaseLLMClient)
  2. Add it to the PROVIDERS dict below
  3. Add the config fields to app/settings.py
"""

from __future__ import annotations
import structlog
from app.llm.base_client import BaseLLMClient
from app.settings import get_settings

log = structlog.get_logger(__name__)

# Registry — add new providers here
def _build_registry() -> dict[str, type[BaseLLMClient]]:
    from app.llm.copilot_client import CopilotClient
    from app.llm.claude_client  import ClaudeClient
    from app.llm.openai_client  import OpenAIClient
    from app.llm.ollama_client  import OllamaClient

    return {
        "copilot": CopilotClient,
        "claude":  ClaudeClient,
        "openai":  OpenAIClient,
        "ollama":  OllamaClient,
    }


_client: BaseLLMClient | None = None


def get_llm_client() -> BaseLLMClient:
    """
    Returns a singleton LLM client for the configured provider.
    Reads LLM_PROVIDER from .env on first call.
    """
    global _client
    if _client is None:
        cfg      = get_settings()
        provider = cfg.llm_provider.lower().strip()
        registry = _build_registry()

        if provider not in registry:
            available = ", ".join(registry.keys())
            raise ValueError(
                f"Unknown LLM_PROVIDER='{provider}'. "
                f"Available options: {available}"
            )

        _client = registry[provider]()
        log.info("llm.provider.loaded", provider=provider)

    return _client


def reset_client() -> None:
    """Force a new client to be created on next call (useful for testing)."""
    global _client
    _client = None


_MODEL_FIELD_BY_PROVIDER = {
    "copilot": "copilot_model",
    "claude":  "claude_model",
    "openai":  "openai_model",
    "ollama":  "ollama_model",
}


def get_active_model_label() -> str:
    """Human-readable 'provider (model)' string for whichever LLM_PROVIDER is
    actually configured — used anywhere the UI displays which model produced
    a review, instead of a hardcoded provider name."""
    cfg = get_settings()
    provider = cfg.llm_provider.lower().strip()
    field = _MODEL_FIELD_BY_PROVIDER.get(provider)
    model = getattr(cfg, field, None) if field else None
    return f"{provider} ({model})" if model else provider
