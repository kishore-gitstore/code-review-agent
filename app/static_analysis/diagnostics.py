"""
app/static_analysis/diagnostics.py — Common diagnostic model every analyzer
plugin normalizes its native tool output into.

Analyzers (Roslyn, SpotBugs, Ruff, Clang-Tidy, ESLint, ...) each speak a
different native format (JSON, XML, compiler text). DiagnosticIssue is the one
shape they all converge on, so the plugin engine (StaticAnalysisRunner) never
needs to know which tool produced a given finding. The runner converts the
merged batch to ReviewComment — the model the rest of the app (coordinator
dedup/filter, GitLab posting) already consumes — at the static-analysis
boundary, so nothing downstream of the runner had to change.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

from app.utils.models import ReviewComment, Severity

# The vocabulary DiagnosticIssue.severity must use — kept as plain strings
# (not the Severity enum) so analyzer plugins don't need to import app-wide
# models, only this module. Must line up 1:1 with Severity's values.
VALID_SEVERITIES = frozenset({"low", "medium", "high", "critical"})


@dataclass(frozen=True, slots=True)
class DiagnosticIssue:
    tool: str
    rule_id: str
    severity: str
    file: str
    line: Optional[int]
    column: Optional[int]
    message: str

    def __post_init__(self) -> None:
        if self.severity not in VALID_SEVERITIES:
            raise ValueError(
                f"DiagnosticIssue.severity={self.severity!r} must be one of {sorted(VALID_SEVERITIES)}"
            )

    def to_review_comment(self) -> ReviewComment:
        description = self.message
        if self.column is not None:
            description = f"{description} (column {self.column})"
        return ReviewComment(
            agent=f"static:{self.tool}",
            severity=Severity(self.severity),
            file_path=self.file,
            line=self.line,
            title=self.rule_id or self.tool,
            description=description,
            suggestion=None,
        )
