"""
app/settings.py — Central settings. Change LLM_PROVIDER in .env to switch models.
"""

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal


class GitLabInstance(BaseModel):
    url: str
    token: str
    webhook_secret: str = ""


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
    github_token: str = "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
    copilot_model: str = "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"

    # Anthropic Claude
    anthropic_api_key: str = "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
    claude_model: str = "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"

    # OpenAI
    openai_api_key: str = "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
    openai_model: str = "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"

    # Ollama
    ollama_base_url: str = "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
    ollama_model: str = "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
    ollama_timeout: int = 300  # local coder models can easily take minutes per request

    # GitLab — one or more instances, keyed by a short name of your choosing.
    # That name appears in the webhook URL (/webhook/gitlab/{name}) and in the
    # manual-trigger request body, so the agent knows which server+token to use.
    # Configure as a single JSON object via the GITLAB_INSTANCES env var, e.g.:
    #   GITLAB_INSTANCES={"gitlab_com":{"url":"https://gitlab.com","token":"...","webhook_secret":"..."},"internal":{"url":"https://gitlab.internal","token":"...","webhook_secret":"..."}}
    gitlab_instances: dict[str, GitLabInstance] = Field(default_factory=dict)

    # Agent behaviour
    max_diff_lines: int = 2000
    min_severity_to_comment: str = "low"
    post_inline_comments: bool = True
    post_summary_comment: bool = True
    post_progress_comment: bool = True  # transient "review in progress" note, deleted once the review completes

    # Review agents
    enable_code_quality: bool = True
    enable_security: bool = True
    enable_performance: bool = True
    enable_documentation: bool = True

    # Static analysis (Roslyn / ESLint / Ruff / Clippy / Clang-Tidy / SpotBugs)
    enable_static_analysis: bool = True
    static_analysis_timeout: int = 60
    clone_for_static_analysis: bool = True
    dotnet_cmd: str = "dotnet"
    eslint_cmd: str = "eslint"
    ruff_cmd: str = "ruff"
    cargo_cmd: str = "cargo"
    clang_tidy_cmd: str = "clang-tidy"
    clang_tidy_args: str = "-- -std=c++17"  # used only when no compile_commands.json is found under repo root
    spotbugs_cmd: str = "spotbugs"
    spotbugs_classes_dirs: str = "target/classes,build/classes/java/main"  # comma-separated candidates, first existing wins

    # Execution mode — see app/execution/. Governs how much runs concurrently:
    # a CPU-only box forces everything to run one at a time regardless of the
    # max_parallel_* values below (a single model/analyzer instance can't
    # truly parallelise anyway); a GPU/cloud-backed box uses them as-is.
    execution_mode: Literal["auto", "cpu", "gpu"] = "auto"
    max_parallel_reviews: int = 1       # concurrent MR reviews (gpu/cloud mode only)
    max_parallel_analyzers: int = 1     # concurrent static-analysis language groups (gpu/cloud mode only)
    max_parallel_llm_requests: int = 1  # concurrent LLM requests, across agents and MRs (gpu/cloud mode only)

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
