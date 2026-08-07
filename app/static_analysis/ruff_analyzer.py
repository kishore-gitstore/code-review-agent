"""
app/static_analysis/ruff_analyzer.py — Python via Ruff.
"""

from __future__ import annotations
import json
import structlog

from .base_analyzer import StaticAnalyzer
from .diagnostics import DiagnosticIssue
from .language_detector import Language
from .registry import register_analyzer

log = structlog.get_logger(__name__)


@register_analyzer(Language.python)
class RuffAnalyzer(StaticAnalyzer):
    tool_name = "ruff"
    language = Language.python

    def __init__(self, cmd: str = "ruff"):
        self.cmd = cmd

    @classmethod
    def from_settings(cls, cfg) -> "RuffAnalyzer":
        return cls(cfg.ruff_cmd)

    async def analyze(self, files: list[str], repo_root: str, timeout: int) -> list[DiagnosticIssue]:
        if not files:
            return []

        cmd = [self.cmd, "check", "--output-format", "json", *files]
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
        for item in results:
            code = item.get("code") or ""
            location = item.get("location") or {}
            issues.append(DiagnosticIssue(
                tool=self.tool_name,
                rule_id=code or "ruff",
                severity=_severity(code),
                file=item.get("filename", "unknown"),
                line=location.get("row"),
                column=location.get("column"),
                message=item.get("message", ""),
            ))
        return issues


def _severity(code: str) -> str:
    if code.startswith("E9") or code.startswith("F"):
        return "high"
    if code.startswith("S"):
        return "high"
    return "medium"
