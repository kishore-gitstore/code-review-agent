"""
app/static_analysis/spotbugs_analyzer.py — Java via SpotBugs.

Unlike the other analyzers, SpotBugs analyzes compiled *bytecode*, not source
— it needs a project that's already been built (Maven's target/classes,
Gradle's build/classes/java/main, ...). We don't attempt to compile the
project ourselves (unlike RoslynAnalyzer's throwaway-project fallback: a C#
project can often be built standalone against just the .NET SDK, but a real
Java project's classpath depends on external dependencies we have no
visibility into), so if none of SPOTBUGS_CLASSES_DIRS exist under repo_root,
this reports unavailable rather than silently returning zero findings.

SpotBugs analyzes the whole classes directory (it's a whole-program analyzer,
there's no "check just these files" mode) and reports issues by class, so
findings are filtered down to bug instances whose source file matches one of
the changed files before being returned.
"""

from __future__ import annotations
import os
import tempfile
import structlog
import xml.etree.ElementTree as ET

from .base_analyzer import StaticAnalyzer, ToolUnavailable
from .diagnostics import DiagnosticIssue
from .language_detector import Language
from .registry import register_analyzer

log = structlog.get_logger(__name__)

_PRIORITY_SEVERITY = {
    "1": "high",
    "2": "medium",
    "3": "low",
}


@register_analyzer(Language.java)
class SpotBugsAnalyzer(StaticAnalyzer):
    tool_name = "spotbugs"
    language = Language.java

    def __init__(self, cmd: str = "spotbugs", classes_dirs: str = "target/classes,build/classes/java/main"):
        self.cmd = cmd
        self.classes_dirs = [d.strip() for d in classes_dirs.split(",") if d.strip()]

    @classmethod
    def from_settings(cls, cfg) -> "SpotBugsAnalyzer":
        return cls(cfg.spotbugs_cmd, cfg.spotbugs_classes_dirs)

    async def analyze(self, files: list[str], repo_root: str, timeout: int) -> list[DiagnosticIssue]:
        if not files:
            return []

        classes_dir = self._find_classes_dir(repo_root)
        if classes_dir is None:
            raise ToolUnavailable(
                f"no compiled classes found under repo root (checked: {', '.join(self.classes_dirs)}) "
                "— build the project (mvn compile / gradle compileJava) before running SpotBugs"
            )

        wanted = [f.replace("\\", "/") for f in files]

        with tempfile.TemporaryDirectory() as report_dir:
            report_path = os.path.join(report_dir, "spotbugs-report.xml")
            cmd = [
                self.cmd, "-textui", "-effort:default", "-low",
                "-xml:withMessages", "-output", report_path,
                classes_dir,
            ]
            _code, _out, err = await self._run(cmd, cwd=repo_root, timeout=timeout)

            if not os.path.exists(report_path):
                if err.strip():
                    log.warning("static_analysis.error", tool=self.tool_name, error=err.strip()[:500])
                return []

            try:
                root = ET.parse(report_path).getroot()
            except ET.ParseError:
                log.warning("static_analysis.parse_error", tool=self.tool_name, raw=err[:300])
                return []

        issues = []
        for bug in root.findall("BugInstance"):
            source_line = bug.find("SourceLine")
            if source_line is None:
                continue
            sourcepath = source_line.get("sourcepath", "")
            matched_file = _match_source_file(sourcepath, wanted)
            if matched_file is None:
                continue

            long_message = bug.find("LongMessage")
            short_message = bug.find("ShortMessage")
            message = (long_message.text if long_message is not None else None) \
                or (short_message.text if short_message is not None else None) or ""

            issues.append(DiagnosticIssue(
                tool=self.tool_name,
                rule_id=bug.get("type", "spotbugs"),
                severity=_PRIORITY_SEVERITY.get(bug.get("priority", "2"), "medium"),
                file=matched_file,
                line=int(source_line.get("start")) if source_line.get("start") else None,
                column=None,
                message=message,
            ))
        return issues

    def _find_classes_dir(self, repo_root: str) -> str | None:
        for rel in self.classes_dirs:
            candidate = os.path.join(repo_root, rel)
            if os.path.isdir(candidate):
                return candidate
        return None


def _match_source_file(sourcepath: str, wanted: list[str]) -> str | None:
    """SpotBugs reports package-relative paths (e.g. com/example/Foo.java);
    changed files are repo-relative (e.g. src/main/java/com/example/Foo.java).
    Match by suffix rather than requiring an exact path."""
    if not sourcepath:
        return None
    for candidate in wanted:
        if candidate.endswith(sourcepath):
            return candidate
    return None
