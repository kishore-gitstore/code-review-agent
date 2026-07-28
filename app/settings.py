"""
app/settings.py — Central settings. Change LLM_PROVIDER in .env to switch models.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Provider switch: copilot | claude | openai | ollama
    llm_provider: str = "ollama"

    # GitHub Copilot
    github_token: str = "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"
    copilot_model: str = "claude-sonnet-4-5"

    # Anthropic Claude
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"

    # Ollama
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "deepseek-coder-v2"
    ollama_timeout: int = 120

    # GitLab
    gitlab_url: str = "http://172.29.242.215/osa/my-test-project"
    gitlab_token: str = "y_AzUekz1A2tASaruTd-"
    gitlab_webhook_secret: str = "supersecret12345"

    # Agent behaviour
    max_diff_lines: int = 2000
    min_severity_to_comment: str = "low"
    post_inline_comments: bool = True
    post_summary_comment: bool = True

    # Review agents
    enable_code_quality: bool = True
    enable_security: bool = True
    enable_performance: bool = True
    enable_documentation: bool = True

    # Server
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
