"""
app/static_analysis/eslint_analyzer.py — JavaScript / TypeScript via ESLint.
"""

from __future__ import annotations
import json
import os
import structlog

from .base_analyzer import StaticAnalyzer
from .diagnostics import DiagnosticIssue
from .language_detector import Language
from .registry import register_analyzer

log = structlog.get_logger(__name__)


@register_analyzer(Language.javascript, Language.typescript)
class ESLintAnalyzer(StaticAnalyzer):
    tool_name = "eslint"
    language = Language.javascript  # runner registers this instance for TS too

    def __init__(self, cmd: str = "eslint"):
        self.cmd = cmd

    @classmethod
    def from_settings(cls, cfg) -> "ESLintAnalyzer":
        return cls(cfg.eslint_cmd)

    async def analyze(self, files: list[str], repo_root: str, timeout: int) -> list[DiagnosticIssue]:
        if not files:
            return []

        cmd = [self.cmd, "--format", "json", "--no-error-on-unmatched-pattern", *files]
        _code, out, err = await self._run(cmd, cwd=repo_root, timeout=timeout)

        if not out.strip():
            if err.strip():
                log.warning("static_analysis.stderr", tool=self.tool_name, error=err[:300])
            return []

        try:
            results = json.loads(out)
        except json.JSONDecodeError:
            log.warning("static_analysis.parse_error", tool=self.tool_name, raw=out[:300])
            return []

        issues = []
        for file_result in results:
            path = _relpath(file_result.get("filePath", "unknown"), repo_root)
            for m in file_result.get("messages", []):
                issues.append(DiagnosticIssue(
                    tool=self.tool_name,
                    rule_id=m.get("ruleId") or "eslint",
                    severity="high" if m.get("severity") == 2 else "medium",
                    file=path,
                    line=m.get("line"),
                    column=m.get("column"),
                    message=m.get("message", ""),
                ))
        return issues


def _relpath(path: str, repo_root: str) -> str:
    try:
        return os.path.relpath(path, repo_root).replace("\\", "/")
    except ValueError:
        return path
