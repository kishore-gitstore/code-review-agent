"""
tests/test_llm_factory.py — get_active_model_label() reflects whichever
LLM_PROVIDER is actually configured, instead of a hardcoded provider name.
"""

from unittest.mock import MagicMock, patch

from app.llm.factory import get_active_model_label


def test_label_reflects_ollama_provider_and_model():
    cfg = MagicMock(llm_provider="ollama", ollama_model="qwen3-coder:latest")
    with patch("app.llm.factory.get_settings", return_value=cfg):
        assert get_active_model_label() == "ollama (qwen3-coder:latest)"


def test_label_reflects_claude_provider_and_model():
    cfg = MagicMock(llm_provider="claude", claude_model="claude-sonnet-5")
    with patch("app.llm.factory.get_settings", return_value=cfg):
        assert get_active_model_label() == "claude (claude-sonnet-5)"


def test_label_falls_back_to_provider_name_when_model_unset():
    cfg = MagicMock(llm_provider="mystery")
    with patch("app.llm.factory.get_settings", return_value=cfg):
        assert get_active_model_label() == "mystery"
