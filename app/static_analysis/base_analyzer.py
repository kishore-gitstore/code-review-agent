"""
app/static_analysis/base_analyzer.py — Common plugin interface every static
analyzer implements. Each concrete analyzer shells out to a real CLI tool
(Roslyn via dotnet, SpotBugs, Ruff, Clang-Tidy, ESLint, ...) and turns its
native output format into a list[DiagnosticIssue] — the one shape the runner
and everything downstream of it actually consumes.
"""

from __future__ import annotations
import structlog
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from app.utils.process_runner import ProcessNotFound, run_process
from .diagnostics import DiagnosticIssue
from .language_detector import Language

if TYPE_CHECKING:
    from app.settings import Settings

log = structlog.get_logger(__name__)


class ToolUnavailable(Exception):
    """Raised when the underlying CLI binary isn't installed / on PATH, or a
    prerequisite the analyzer needs (a project file, a build output
    directory, ...) isn't present."""


class StaticAnalyzer(ABC):
    """
    Plugin interface. A new analyzer is: subclass this, implement analyze()
    and from_settings(), decorate the class with @register_analyzer(...) in
    registry.py. StaticAnalysisRunner never needs to change.
    """

    tool_name: str
    language: Language

    @classmethod
    @abstractmethod
    def from_settings(cls, cfg: "Settings") -> "StaticAnalyzer":
        """Construct an instance purely from app settings — the runner calls
        this uniformly, without knowing any analyzer's constructor signature."""
        ...

    @abstractmethod
    async def analyze(self, files: list[str], repo_root: str, timeout: int) -> list[DiagnosticIssue]:
        """Run the tool against `files` (paths relative to repo_root) and
        return findings normalized to DiagnosticIssue."""
        ...

    async def _run(self, cmd: list[str], cwd: str, timeout: int):
        """Run `cmd`, returning (returncode, stdout, stderr). Never raises on
        a non-zero exit — most linters exit non-zero simply because they
        found issues. Delegates to the shared process runner (also used by
        hardware detection) so every analyzer gets the same timeout/
        cancellation handling and structured logging for free."""
        try:
            result = await run_process(cmd, cwd=cwd, timeout=timeout, tool_name=self.tool_name)
        except ProcessNotFound as exc:
            raise ToolUnavailable(str(exc)) from exc
        return result.returncode, result.stdout, result.stderr
