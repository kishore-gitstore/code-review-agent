"""
app/static_analysis/clang_tidy_analyzer.py — C / C++ via clang-tidy.

clang-tidy needs to resolve #includes to do anything beyond the most trivial
checks, which normally means a compilation database (compile_commands.json,
produced by CMake/Bazel/etc. or `bear`/`compiledb`). If one exists anywhere
under repo_root, it's used automatically (`-p <dir>`); otherwise we fall back
to CLANG_TIDY_ARGS (default `-- -std=c++17`) so at least syntax-only checks
still run. Point CLANG_TIDY_ARGS at your own flags if the fallback isn't
enough for your codebase.
"""

from __future__ import annotations
import os
import re
import shlex
import structlog

from .base_analyzer import StaticAnalyzer
from .diagnostics import DiagnosticIssue
from .language_detector import Language
from .registry import register_analyzer

log = structlog.get_logger(__name__)

# clang-tidy diagnostic line: "<file>:<line>:<column>: <severity>: <message> [<check-name>]"
_DIAGNOSTIC_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<column>\d+):\s*(?P<severity>warning|error|note):\s*"
    r"(?P<message>.+?)(?:\s*\[(?P<check>[\w.\-,]+)\])?$"
)


@register_analyzer(Language.cpp)
class ClangTidyAnalyzer(StaticAnalyzer):
    tool_name = "clang-tidy"
    language = Language.cpp

    def __init__(self, cmd: str = "clang-tidy", extra_args: str = "-- -std=c++17"):
        self.cmd = cmd
        self.extra_args = extra_args

    @classmethod
    def from_settings(cls, cfg) -> "ClangTidyAnalyzer":
        return cls(cfg.clang_tidy_cmd, cfg.clang_tidy_args)

    async def analyze(self, files: list[str], repo_root: str, timeout: int) -> list[DiagnosticIssue]:
        if not files:
            return []

        compile_db_dir = _find_compile_db_dir(repo_root)
        cmd = [self.cmd, "--quiet", *files]
        if compile_db_dir:
            cmd += ["-p", compile_db_dir]
        elif self.extra_args.strip():
            cmd += shlex.split(self.extra_args)

        _code, out, err = await self._run(cmd, cwd=repo_root, timeout=timeout)

        text = out if out.strip() else err
        if not text.strip():
            return []

        issues = []
        for line in text.splitlines():
            match = _DIAGNOSTIC_RE.match(line.strip())
            if not match or match.group("severity") == "note":
                continue
            path = _relpath(match.group("file"), repo_root)
            issues.append(DiagnosticIssue(
                tool=self.tool_name,
                rule_id=match.group("check") or "clang-tidy",
                severity="high" if match.group("severity") == "error" else "medium",
                file=path,
                line=int(match.group("line")),
                column=int(match.group("column")),
                message=match.group("message"),
            ))
        return issues


def _relpath(path: str, repo_root: str) -> str:
    normalized = os.path.normpath(path).replace("\\", "/")
    if not os.path.isabs(path):
        return normalized
    try:
        return os.path.relpath(path, repo_root).replace("\\", "/")
    except ValueError:
        return normalized


def _find_compile_db_dir(repo_root: str) -> str | None:
    for dirpath, _dirs, filenames in os.walk(repo_root):
        if "compile_commands.json" in filenames:
            return dirpath
    return None
