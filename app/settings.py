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
    # Render a finding's code fix as a GitLab ```suggestion block, which gives
    # the MR author a one-click "Apply suggestion" button that commits it.
    # Off → the fix is shown as a plain read-only code block instead.
    post_fix_suggestions: bool = True
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

    # ── Dashboard authentication (Active Directory over LDAP) ──────────────
    # Guards the reporting surface only: the dashboard page, the report APIs,
    # and the endpoints that start or cancel a review. GitLab webhooks are
    # NOT covered — they authenticate with their own shared secret, and
    # putting a login in front of them would break every hook.
    #
    # false leaves the dashboard open to anyone who can reach the port; keep
    # it that way only on a dev box.
    auth_enabled: bool = True

    # Domain controller to bind against. Bare host, host:port, or a URL —
    # ldaps://dc01.corp.local forces TLS regardless of ldap_use_ssl.
    ldap_server: str = ""
    ldap_port: int = 0              # 0 → 636 when ldap_use_ssl else 389
    ldap_use_ssl: bool = True       # plain LDAP sends the password in clear text
    ldap_timeout: int = 10

    # How a bare login name ("bkishorkumar") is turned into something AD will
    # bind as: "upn" → name@ldap_domain (needs ldap_domain), "ntlm" →
    # NETBIOS\name (needs ldap_netbios_domain, or the first label of
    # ldap_domain). A name the user types already qualified is left alone.
    ldap_bind_style: Literal["upn", "ntlm"] = "upn"
    ldap_domain: str = ""           # corp.local
    ldap_netbios_domain: str = ""   # CORP — only needed for ldap_bind_style=ntlm

    # Where to look the user up after the bind, for display name and groups.
    # Optional: leave base_dn empty and logins still work, you just lose the
    # real name in the UI and can't use ldap_required_group.
    ldap_base_dn: str = ""          # DC=corp,DC=local
    ldap_user_filter: str = "(&(objectClass=user)(sAMAccountName={username}))"

    # Restrict the dashboard to one AD group. Empty = any account that can
    # bind. A bare CN ("CRA-Dashboard-Users") matches direct membership only;
    # a full DN also matches nested groups via AD's LDAP_MATCHING_RULE_IN_CHAIN.
    ldap_required_group: str = ""

    # Who may open the configuration page and start/stop the review service.
    # Either list usernames (comma-separated, case-insensitive) or name an AD
    # group (CN). Both empty = nobody is an admin while auth is on — the safe
    # default; the pages stay locked until you name someone. With
    # AUTH_ENABLED=false everyone is admin, as there is no identity to check.
    admin_users: str = ""
    admin_group: str = ""

    # Signs the session cookie. Generate with:
    #   python -c "import secrets; print(secrets.token_urlsafe(48))"
    # Leaving it empty generates a random one per process — logins then die on
    # every service restart and don't work across multiple instances.
    session_secret: str = ""
    session_ttl_hours: int = 8
    session_cookie_name: str = "cra_session"
    session_cookie_secure: bool = False  # true once the dashboard is served over HTTPS

    # Optional shared secret for non-browser callers (CI, monitoring) sent as
    # an X-API-Token header. Empty disables it — sessions become the only way in.
    dashboard_api_token: str = ""


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    """Re-read .env (used after the configuration page saves it)."""
    global _settings
    _settings = Settings()
    return _settings
