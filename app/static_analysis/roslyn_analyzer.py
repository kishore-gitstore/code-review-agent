"""
app/static_analysis/roslyn_analyzer.py — C# via Roslyn analyzers, driven through
the `dotnet format analyzers` CLI (ships with the .NET SDK, no extra install).

Two things this analyzer has to work around, confirmed empirically against a
real dotnet 10 SDK install:

1. `dotnet format analyzers` silently reports ZERO diagnostics when pointed
   directly at a bare .csproj (logs "Skipping referenced project '<name>'"
   even though the exact same project reports the same warnings fine via
   `dotnet build`). It only works reliably against a solution. So every
   .csproj this analyzer touches — whether a real one found on disk or one we
   synthesize — gets wrapped in a throwaway solution file first.
2. `dotnet new sln` on newer SDKs (10+) defaults to the new XML `.slnx`
   format instead of the classic `.sln` text format, so a repo using modern
   tooling may only have a `.slnx` on disk — `_find_project` has to look for
   both.

Roslyn/MSBuild needs a project to resolve references at all, so when no real
.sln/.slnx/.csproj exists under repo_root — e.g. only the changed file(s) were
fetched via the GitLab API rather than a full git clone — this synthesizes a
minimal, throwaway .csproj around just those files. That only sees the changed
files, so it won't catch cross-file issues a full project/clone would.
"""

from __future__ import annotations
import json
import os
import shutil
import tempfile
import structlog

from .base_analyzer import StaticAnalyzer, ToolUnavailable
from .diagnostics import DiagnosticIssue
from .language_detector import Language
from .registry import register_analyzer

log = structlog.get_logger(__name__)

_DEFAULT_TFM = "net8.0"
_SOLUTION_EXTENSIONS = (".sln", ".slnx")


@register_analyzer(Language.csharp)
class RoslynAnalyzer(StaticAnalyzer):
    tool_name = "roslyn"
    language = Language.csharp

    def __init__(self, dotnet_cmd: str = "dotnet"):
        self.dotnet_cmd = dotnet_cmd

    @classmethod
    def from_settings(cls, cfg) -> "RoslynAnalyzer":
        return cls(cfg.dotnet_cmd)

    async def analyze(self, files: list[str], repo_root: str, timeout: int) -> list[DiagnosticIssue]:
        if not files:
            return []

        wanted = set(files)
        project = _find_project(repo_root)

        if project and project.endswith(_SOLUTION_EXTENSIONS):
            return await self._analyze_against(
                project, cwd=repo_root, analysis_root=repo_root, wanted=wanted, timeout=timeout
            )

        with tempfile.TemporaryDirectory() as work_dir:
            if project:
                # Found a bare .csproj with no solution alongside it — wrap it
                # in a throwaway solution (via an absolute path reference) so
                # dotnet format actually reports diagnostics. This never
                # touches repo_root; it just points at the real csproj from
                # an isolated temp dir.
                slnx = _write_slnx(project, work_dir)
                return await self._analyze_against(
                    slnx, cwd=work_dir, analysis_root=repo_root, wanted=wanted, timeout=timeout
                )

            csproj = await self._synthesize_project(files, repo_root, work_dir, timeout)
            if not csproj:
                raise ToolUnavailable("no .sln/.slnx/.csproj found under repo root, "
                                       "and none of the changed files could be copied to synthesize one")
            slnx = _write_slnx(csproj, work_dir)
            return await self._analyze_against(
                slnx, cwd=work_dir, analysis_root=work_dir, wanted=wanted, timeout=timeout
            )

    async def _analyze_against(
        self, solution: str, cwd: str, analysis_root: str, wanted: set[str], timeout: int,
    ) -> list[DiagnosticIssue]:
        with tempfile.TemporaryDirectory() as report_dir:
            cmd = [
                self.dotnet_cmd, "format", "analyzers", solution,
                "--verify-no-changes", "--report", report_dir,
                "--severity", "info",
            ]
            _code, _out, err = await self._run(cmd, cwd=cwd, timeout=timeout)

            report_path = os.path.join(report_dir, "format-report.json")
            if not os.path.exists(report_path):
                if err.strip():
                    log.warning("static_analysis.error", tool=self.tool_name, error=err.strip()[:500])
                return []

            try:
                with open(report_path, encoding="utf-8") as f:
                    entries = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("static_analysis.parse_error", tool=self.tool_name, error=str(exc))
                return []

        # dotnet/MSBuild reports FilePath through .NET's own path resolution,
        # which can disagree byte-for-byte with Python's (e.g. Windows 8.3
        # short names like `BKISHO~1` vs the long form) even when they point
        # at the identical file. realpath() normalizes both sides to the same
        # canonical form before comparing — without it, every entry silently
        # fails the `path not in wanted` check below and gets dropped.
        analysis_root = os.path.realpath(analysis_root)

        comments = []
        for entry in entries:
            raw_path = entry.get("FilePath", "unknown")
            path = os.path.relpath(os.path.realpath(raw_path), analysis_root).replace("\\", "/")
            if path not in wanted:
                continue
            for change in entry.get("FileChanges", []):
                diag_id = change.get("DiagnosticId", "roslyn")
                comments.append(DiagnosticIssue(
                    tool=self.tool_name,
                    rule_id=diag_id,
                    severity=_severity(diag_id),
                    file=path,
                    line=change.get("LineNumber"),
                    column=None,
                    message=change.get("FormatDescription", ""),
                ))
        return comments

    async def _synthesize_project(
        self, files: list[str], repo_root: str, project_dir: str, timeout: int,
    ) -> str | None:
        copied = False
        for rel in files:
            src = os.path.join(repo_root, rel)
            if not os.path.exists(src):
                continue
            dest = os.path.join(project_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
            copied = True

        if not copied:
            return None

        tfm = await self._detect_tfm(repo_root, timeout)
        # Named without underscores/leading punctuation so the synthetic
        # project's own name doesn't itself trip a naming-convention rule
        # (e.g. CA1707) and show up as a false "finding".
        csproj_path = os.path.join(project_dir, "ReviewProject.csproj")
        with open(csproj_path, "w", encoding="utf-8") as f:
            f.write(
                "<Project Sdk=\"Microsoft.NET.Sdk\">\n"
                "  <PropertyGroup>\n"
                f"    <TargetFramework>{tfm}</TargetFramework>\n"
                "    <Nullable>enable</Nullable>\n"
                "    <EnableNETAnalyzers>true</EnableNETAnalyzers>\n"
                "    <AnalysisLevel>latest</AnalysisLevel>\n"
                "    <EnforceCodeStyleInBuild>true</EnforceCodeStyleInBuild>\n"
                # This is a throwaway analysis shim, not a real entry point —
                # don't fail the build over a missing Main/duplicate-attribute etc.
                "    <NoWarn>CS5001;CS0246</NoWarn>\n"
                "  </PropertyGroup>\n"
                "</Project>\n"
            )
        return csproj_path

    async def _detect_tfm(self, repo_root: str, timeout: int) -> str:
        """Match the synthetic project's target framework to whatever SDK is
        actually installed (e.g. dotnet 10 -> net10.0) instead of hardcoding a
        version that might not be present on this machine."""
        try:
            _code, out, _err = await self._run([self.dotnet_cmd, "--version"], cwd=repo_root, timeout=timeout)
            major = out.strip().split(".")[0]
            if major.isdigit():
                return f"net{major}.0"
        except Exception as exc:
            log.warning("static_analysis.tfm_detect_failed", tool=self.tool_name, error=str(exc))
        return _DEFAULT_TFM


def _write_slnx(csproj_path: str, slnx_dir: str) -> str:
    """Wrap a bare .csproj in a minimal throwaway solution — dotnet format
    only reliably reports analyzer diagnostics against a solution, not a bare
    project file. Uses an absolute path so the .slnx can live anywhere,
    independent of where the real project sits."""
    slnx_path = os.path.join(slnx_dir, "ReviewProject.slnx")
    with open(slnx_path, "w", encoding="utf-8") as f:
        f.write(
            "<Solution>\n"
            f"  <Project Path=\"{os.path.abspath(csproj_path)}\" />\n"
            "</Solution>\n"
        )
    return slnx_path


def _find_project(repo_root: str) -> str | None:
    for dirpath, _dirs, filenames in os.walk(repo_root):
        for name in filenames:
            if name.endswith(_SOLUTION_EXTENSIONS):
                return os.path.join(dirpath, name)
    for dirpath, _dirs, filenames in os.walk(repo_root):
        for name in filenames:
            if name.endswith(".csproj"):
                return os.path.join(dirpath, name)
    return None


def _severity(diagnostic_id: str) -> str:
    if diagnostic_id.startswith("CS"):
        return "critical"
    if diagnostic_id.startswith("CA"):
        return "high"
    return "low"
