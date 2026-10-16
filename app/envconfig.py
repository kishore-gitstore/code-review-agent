"""
app/envconfig.py — read and write the .env file behind the configuration page.

Only the keys listed in SCHEMA can be changed, so the page can't be used to
inject arbitrary variables (PATH, PYTHONPATH…) into the service's environment.
Edits are made in place: comments, blank lines, ordering and every key the
schema doesn't know about are preserved, and the file is replaced atomically so
a crash mid-write can't leave a half-written .env that stops the service
booting.

Secrets never leave the server. They are returned as SECRET_MASK, and a value
posted back as SECRET_MASK means "unchanged". GITLAB_INSTANCES is JSON with a
token and webhook secret inside it, so those two fields are masked and
re-merged per instance rather than masking the whole value.
"""

from __future__ import annotations
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

SECRET_MASK = "********"
ENV_PATH = Path(".env")


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str = "text"          # text | number | bool | select | secret | json
    options: tuple[str, ...] = ()
    help: str = ""
    restart: bool = False       # only takes effect after a process restart


def _f(*a, **k) -> Field:
    return Field(*a, **k)


SCHEMA: tuple[tuple[str, tuple[Field, ...]], ...] = (
    ("LLM provider", (
        _f("LLM_PROVIDER", "Provider", "select", ("ollama", "claude", "openai", "copilot")),
        _f("OLLAMA_BASE_URL", "Ollama URL"),
        _f("OLLAMA_MODEL", "Ollama model"),
        _f("OLLAMA_TIMEOUT", "Ollama timeout (s)", "number"),
        _f("ANTHROPIC_API_KEY", "Anthropic API key", "secret"),
        _f("CLAUDE_MODEL", "Claude model"),
        _f("OPENAI_API_KEY", "OpenAI API key", "secret"),
        _f("OPENAI_MODEL", "OpenAI model"),
        _f("GITHUB_TOKEN", "GitHub token (Copilot)", "secret"),
        _f("COPILOT_MODEL", "Copilot model"),
    )),
    ("GitLab", (
        _f("GITLAB_INSTANCES", "GitLab instances", "json",
           help='JSON object keyed by instance name: {"name": {"url": "...", "token": "...", "webhook_secret": "..."}}. '
                'Leave a token as ******** to keep the saved one.'),
    )),
    ("Review behaviour", (
        _f("MAX_DIFF_LINES", "Max diff lines", "number"),
        _f("MIN_SEVERITY_TO_COMMENT", "Minimum severity to comment", "select",
           ("low", "medium", "high", "critical")),
        _f("POST_INLINE_COMMENTS", "Post inline comments", "bool"),
        _f("POST_SUMMARY_COMMENT", "Post summary comment", "bool"),
        _f("POST_FIX_SUGGESTIONS", "Post fix suggestions", "bool"),
        _f("POST_PROGRESS_COMMENT", "Post “in progress” note", "bool"),
        _f("ENABLE_CODE_QUALITY", "Code quality agent", "bool"),
        _f("ENABLE_SECURITY", "Security agent", "bool"),
        _f("ENABLE_PERFORMANCE", "Performance agent", "bool"),
        _f("ENABLE_DOCUMENTATION", "Documentation agent", "bool"),
    )),
    ("Execution", (
        _f("EXECUTION_MODE", "Execution mode", "select", ("auto", "cpu", "gpu")),
        _f("MAX_PARALLEL_REVIEWS", "Parallel reviews", "number"),
        _f("MAX_PARALLEL_ANALYZERS", "Parallel analyzers", "number"),
        _f("MAX_PARALLEL_LLM_REQUESTS", "Parallel LLM requests", "number"),
    )),
    ("Static analysis", (
        _f("ENABLE_STATIC_ANALYSIS", "Enable static analysis", "bool"),
        _f("CLONE_FOR_STATIC_ANALYSIS", "Clone repo for analysis", "bool"),
        _f("STATIC_ANALYSIS_TIMEOUT", "Timeout (s)", "number"),
        _f("DOTNET_CMD", "dotnet command"),
        _f("ESLINT_CMD", "eslint command"),
        _f("RUFF_CMD", "ruff command"),
        _f("CARGO_CMD", "cargo command"),
        _f("CLANG_TIDY_CMD", "clang-tidy command"),
        _f("CLANG_TIDY_ARGS", "clang-tidy args"),
        _f("SPOTBUGS_CMD", "spotbugs command"),
        _f("SPOTBUGS_CLASSES_DIRS", "SpotBugs class dirs"),
    )),
    ("Sign-in & access", (
        _f("AUTH_ENABLED", "Require sign-in", "bool",
           help="Turning this off lets anyone who can reach the port use the dashboard and admin pages."),
        _f("ADMIN_USERS", "Admin usernames", help="Comma-separated AD usernames allowed to configure and start/stop the service."),
        _f("ADMIN_GROUP", "Admin AD group", help="Members of this AD group (CN) are admins too."),
        _f("LDAP_SERVER", "LDAP server"),
        _f("LDAP_PORT", "LDAP port", "number"),
        _f("LDAP_USE_SSL", "LDAP over SSL", "bool"),
        _f("LDAP_TIMEOUT", "LDAP timeout (s)", "number"),
        _f("LDAP_BIND_STYLE", "Bind style", "select", ("upn", "ntlm")),
        _f("LDAP_DOMAIN", "LDAP domain"),
        _f("LDAP_NETBIOS_DOMAIN", "NetBIOS domain"),
        _f("LDAP_BASE_DN", "Base DN"),
        _f("LDAP_USER_FILTER", "User filter"),
        _f("LDAP_REQUIRED_GROUP", "Required group"),
        _f("SESSION_SECRET", "Session secret", "secret",
           help="Changing it signs everyone out."),
        _f("SESSION_TTL_HOURS", "Session length (hours)", "number"),
        _f("SESSION_COOKIE_SECURE", "Secure cookie (HTTPS only)", "bool"),
        _f("DASHBOARD_API_TOKEN", "API token", "secret"),
    )),
    ("Server", (
        _f("APP_HOST", "Host", restart=True),
        _f("APP_PORT", "Port", "number", restart=True),
        _f("LOG_LEVEL", "Log level", "select", ("DEBUG", "INFO", "WARNING", "ERROR"), restart=True),
    )),
)

FIELDS: dict[str, Field] = {f.key: f for _, fields in SCHEMA for f in fields}


class ConfigError(ValueError):
    """A submitted value is invalid; the message is shown to the admin."""


# ── Reading ──────────────────────────────────────────────────────────────────

_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        inner = raw[1:-1]
        if raw[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    # Unquoted: a trailing " # comment" is not part of the value.
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def _scan(lines: list[str]):
    """Yield (key, first_line_idx, last_line_idx, raw_value) per assignment,
    following a quoted value across lines (GITLAB_INSTANCES is multi-line)."""
    i = 0
    while i < len(lines):
        m = _KEY_RE.match(lines[i])
        if not m or lines[i].lstrip().startswith("#"):
            i += 1
            continue
        key, rest = m.group(1), m.group(2).strip()
        end, value = i, rest
        if rest[:1] in ("'", '"') and not (len(rest) >= 2 and rest.endswith(rest[0])):
            q = rest[0]
            while end + 1 < len(lines):
                end += 1
                value += "\n" + lines[end].rstrip("\r\n")
                if lines[end].rstrip().endswith(q):
                    break
        yield key, i, end, value
        i = end + 1


def read_values(path: Path | None = None) -> dict[str, str]:
    """Current .env values for the schema keys ('' when absent)."""
    path = path or ENV_PATH
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out = {k: "" for k in FIELDS}
    for key, _s, _e, raw in _scan(lines):
        if key in FIELDS:
            out[key] = _unquote(raw)
    return out


# ── GITLAB_INSTANCES masking ─────────────────────────────────────────────────

_INSTANCE_SECRETS = ("token", "webhook_secret")


def _mask_instances(raw: str) -> str:
    if not raw.strip():
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return SECRET_MASK  # unparseable: never echo something that may hold a token
    for inst in data.values() if isinstance(data, dict) else []:
        if isinstance(inst, dict):
            for k in _INSTANCE_SECRETS:
                if inst.get(k):
                    inst[k] = SECRET_MASK
    return json.dumps(data, indent=2)


def _merge_instances(new_raw: str, old_raw: str) -> str:
    """Validate the submitted JSON and restore masked secrets from the saved one."""
    if not new_raw.strip():
        return ""
    try:
        new = json.loads(new_raw)
    except ValueError as exc:
        raise ConfigError(f"GITLAB_INSTANCES is not valid JSON: {exc}")
    if not isinstance(new, dict):
        raise ConfigError("GITLAB_INSTANCES must be a JSON object keyed by instance name")
    try:
        old = json.loads(old_raw) if old_raw.strip() else {}
    except ValueError:
        old = {}
    for name, inst in new.items():
        if not isinstance(inst, dict) or not str(inst.get("url", "")).strip():
            raise ConfigError(f"GitLab instance {name!r} needs a url")
        if not str(inst.get("token", "")).strip():
            raise ConfigError(f"GitLab instance {name!r} needs a token")
        for k in _INSTANCE_SECRETS:
            if inst.get(k) == SECRET_MASK:
                saved = (old.get(name) or {}).get(k) if isinstance(old, dict) else None
                if saved is None:
                    raise ConfigError(f"{name}: no saved {k} to keep — enter it")
                inst[k] = saved
    return json.dumps(new, indent=2)


def masked_values(path: Path | None = None) -> dict[str, str]:
    """read_values() with every secret replaced by SECRET_MASK (or '' if unset)."""
    vals = read_values(path)
    for key, f in FIELDS.items():
        if f.kind == "secret" and vals[key]:
            vals[key] = SECRET_MASK
        elif key == "GITLAB_INSTANCES":
            vals[key] = _mask_instances(vals[key])
    return vals


def schema_payload() -> list[dict]:
    return [
        {"title": title, "fields": [
            {"key": f.key, "label": f.label, "kind": f.kind, "options": list(f.options),
             "help": f.help, "restart": f.restart}
            for f in fields]}
        for title, fields in SCHEMA
    ]


# ── Writing ──────────────────────────────────────────────────────────────────

_BARE_OK = re.compile(r"^[A-Za-z0-9_./:@,+\-=]*$")


def _render(key: str, value: str) -> str:
    if f"{key}" == "GITLAB_INSTANCES" and value:
        return f"{key}='{value}'"
    if _BARE_OK.match(value):
        return f"{key}={value}"
    if "'" not in value:
        return f"{key}='{value}'"
    return f'{key}="' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _validate(key: str, value: str) -> str:
    f = FIELDS[key]
    value = value.strip() if f.kind != "json" else value
    if f.kind != "json" and ("\n" in value or "\r" in value):
        raise ConfigError(f"{f.label}: must be a single line")
    if f.kind == "number":
        if not re.fullmatch(r"-?\d+", value):
            raise ConfigError(f"{f.label}: must be a whole number")
    elif f.kind == "bool":
        value = value.lower()
        if value not in ("true", "false"):
            raise ConfigError(f"{f.label}: must be true or false")
    elif f.kind == "select":
        if value not in f.options:
            raise ConfigError(f"{f.label}: must be one of {', '.join(f.options)}")
    return value


def apply_changes(changes: dict[str, str], path: Path | None = None) -> list[str]:
    """
    Write `changes` into the .env file and return the keys that actually
    changed. Unknown keys are rejected outright; secrets posted back as
    SECRET_MASK are skipped (unchanged).
    """
    path = path or ENV_PATH
    unknown = [k for k in changes if k not in FIELDS]
    if unknown:
        raise ConfigError(f"Not a configurable setting: {', '.join(sorted(unknown))}")

    current = read_values(path)
    resolved: dict[str, str] = {}
    for key, raw in changes.items():
        raw = "" if raw is None else str(raw)
        f = FIELDS[key]
        if f.kind == "secret" and raw == SECRET_MASK:
            continue
        value = _merge_instances(raw, current[key]) if f.kind == "json" else _validate(key, raw)
        if value != current[key] and not (f.kind == "json" and _same_json(value, current[key])):
            resolved[key] = value
    if not resolved:
        return []

    # Bytes, not read_text(): text mode would fold CRLF to LF before the
    # line-ending check below could see it.
    text = path.read_bytes().decode("utf-8") if path.exists() else ""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    spans = {key: (s, e) for key, s, e, _ in _scan(lines)}

    # Replace bottom-up so earlier line indexes stay valid.
    appended: list[str] = []
    for key in sorted(resolved, key=lambda k: -spans[k][0] if k in spans else 1):
        rendered = _render(key, resolved[key]).split("\n")
        if key in spans:
            s, e = spans[key]
            lines[s:e + 1] = rendered
        else:
            appended = rendered + appended
    if appended:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(appended)

    data = newline.join(lines) + newline
    fd, tmp = tempfile.mkstemp(dir=str(path.resolve().parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return list(resolved)


def _same_json(a: str, b: str) -> bool:
    try:
        return json.loads(a or "null") == json.loads(b or "null")
    except ValueError:
        return False
