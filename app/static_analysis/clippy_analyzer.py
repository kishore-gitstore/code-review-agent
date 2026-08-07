"""
app/static_analysis/clippy_analyzer.py — Rust via `cargo clippy`.

Clippy operates on the whole crate (there's no "check just these files" mode),
so we run it once per repo and keep only messages whose spans touch a changed file.
"""

from __future__ import annotations
import json
import os
import structlog

from .base_analyzer import StaticAnalyzer, ToolUnavailable
from .diagnostics import DiagnosticIssue
from .language_detector import Language
from .registry import register_analyzer

log = structlog.get_logger(__name__)


@register_analyzer(Language.rust)
class ClippyAnalyzer(StaticAnalyzer):
    tool_name = "clippy"
    language = Language.rust

    def __init__(self, cargo_cmd: str = "cargo"):
        self.cargo_cmd = cargo_cmd

    @classmethod
    def from_settings(cls, cfg) -> "ClippyAnalyzer":
        return cls(cfg.cargo_cmd)

    async def analyze(self, files: list[str], repo_root: str, timeout: int) -> list[DiagnosticIssue]:
        if not files:
            return []
        if not os.path.exists(os.path.join(repo_root, "Cargo.toml")):
            raise ToolUnavailable("no Cargo.toml found under repo root")

        cmd = [self.cargo_cmd, "clippy", "--message-format=json", "--quiet"]
        _code, out, err = await self._run(cmd, cwd=repo_root, timeout=timeout)

        if not out.strip():
            if err.strip():
                log.warning("static_analysis.stderr", tool=self.tool_name, error=err[:300])
            return []

        wanted = set(files)
        issues = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("reason") != "compiler-message":
                continue

            message = payload.get("message") or {}
            spans = message.get("spans") or []
            level = message.get("level", "warning")
            text = message.get("message", "")
            code = (message.get("code") or {}).get("code") or "clippy"

            for span in spans:
                path = span.get("file_name", "").replace("\\", "/")
                if path not in wanted:
                    continue
                issues.append(DiagnosticIssue(
                    tool=self.tool_name,
                    rule_id=code,
                    severity="high" if level == "error" else "medium",
                    file=path,
                    line=span.get("line_start"),
                    column=span.get("column_start"),
                    message=text,
                ))
        return issues
