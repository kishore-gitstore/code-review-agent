"""
tests/test_static_analysis.py — Unit tests for the static analysis pipeline stage
(language detection, plugin registry, per-tool result parsing, and the runner's
dispatch/merge/concurrency logic).

Subprocess calls are mocked out via StaticAnalyzer._run so these tests don't
require eslint/ruff/clang-tidy/cargo/dotnet/spotbugs to actually be installed.
"""

import asyncio
import json
import os
import pytest
from unittest.mock import AsyncMock, patch

from app.utils.models import Severity
from app.execution.policy import ExecutionPolicy
from app.static_analysis.language_detector import Language, detect_language, group_by_language
from app.static_analysis.base_analyzer import ToolUnavailable
from app.static_analysis.diagnostics import DiagnosticIssue
from app.static_analysis.registry import registered_analyzers
from app.static_analysis.eslint_analyzer import ESLintAnalyzer
from app.static_analysis.ruff_analyzer import RuffAnalyzer
from app.static_analysis.clippy_analyzer import ClippyAnalyzer
from app.static_analysis.clang_tidy_analyzer import ClangTidyAnalyzer
from app.static_analysis.spotbugs_analyzer import SpotBugsAnalyzer
from app.static_analysis.roslyn_analyzer import RoslynAnalyzer
from app.static_analysis.runner import StaticAnalysisRunner


# ── Language detector ─────────────────────────────────────────────────────────

def test_detect_language_by_extension():
    assert detect_language("app/foo.py")  == Language.python
    assert detect_language("src/foo.ts")  == Language.typescript
    assert detect_language("src/foo.jsx") == Language.javascript
    assert detect_language("Program.cs")  == Language.csharp
    assert detect_language("main.rs")     == Language.rust
    assert detect_language("lib.cpp")     == Language.cpp
    assert detect_language("Foo.java")    == Language.java
    assert detect_language("README.md")   is None


def test_group_by_language_drops_unsupported_files():
    groups = group_by_language(["a.py", "b.py", "c.ts", "README.md"])
    assert groups[Language.python]     == ["a.py", "b.py"]
    assert groups[Language.typescript] == ["c.ts"]
    assert Language.javascript not in groups


# ── Plugin registry ───────────────────────────────────────────────────────────

def test_registry_maps_every_supported_language_to_its_analyzer():
    analyzers = registered_analyzers()
    assert analyzers[Language.csharp]     is RoslynAnalyzer
    assert analyzers[Language.javascript] is ESLintAnalyzer
    assert analyzers[Language.typescript] is ESLintAnalyzer
    assert analyzers[Language.python]     is RuffAnalyzer
    assert analyzers[Language.rust]       is ClippyAnalyzer
    assert analyzers[Language.cpp]        is ClangTidyAnalyzer
    assert analyzers[Language.java]       is SpotBugsAnalyzer


def test_runner_instantiates_one_analyzer_per_registered_language():
    """The engine (runner) must pick up every registered plugin without any
    per-language special-casing — this is what "new analyzers need zero
    changes to the review engine" actually means in practice."""
    runner = StaticAnalysisRunner()
    assert set(runner._analyzers.keys()) == set(registered_analyzers().keys())


# ── DiagnosticIssue (common model) ────────────────────────────────────────────

def test_diagnostic_issue_converts_to_review_comment():
    issue = DiagnosticIssue(
        tool="ruff", rule_id="F821", severity="high",
        file="app.py", line=3, column=5, message="undefined name 'x'",
    )
    comment = issue.to_review_comment()
    assert comment.agent    == "static:ruff"
    assert comment.severity == Severity.high
    assert comment.file_path == "app.py"
    assert comment.line     == 3
    assert comment.title    == "F821"
    assert "column 5" in comment.description


def test_diagnostic_issue_rejects_invalid_severity():
    with pytest.raises(ValueError):
        DiagnosticIssue(tool="ruff", rule_id="X", severity="fatal",
                         file="app.py", line=1, column=None, message="")


# ── ESLint ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_eslint_analyzer_parses_json(tmp_path):
    analyzer  = ESLintAnalyzer()
    file_path = str(tmp_path / "src" / "app.js")
    output = json.dumps([{
        "filePath": file_path,
        "messages": [
            {"ruleId": "no-unused-vars", "severity": 2, "message": "'x' unused", "line": 5, "column": 3},
            {"ruleId": "quotes",         "severity": 1, "message": "use double quotes", "line": 8, "column": 1},
        ],
    }])

    with patch.object(ESLintAnalyzer, "_run", new=AsyncMock(return_value=(1, output, ""))):
        results = await analyzer.analyze(["src/app.js"], str(tmp_path), 30)

    assert len(results) == 2
    assert results[0].severity == "high"
    assert results[0].rule_id  == "no-unused-vars"
    assert results[0].file     == "src/app.js"
    assert results[0].column   == 3
    assert results[1].severity == "medium"


@pytest.mark.asyncio
async def test_eslint_analyzer_raises_when_tool_missing(tmp_path):
    """ToolUnavailable propagates to the caller (the runner) rather than being
    swallowed here — otherwise a missing tool looks identical to '0 issues found'."""
    analyzer = ESLintAnalyzer()
    with patch.object(ESLintAnalyzer, "_run", new=AsyncMock(side_effect=ToolUnavailable("no eslint"))):
        with pytest.raises(ToolUnavailable):
            await analyzer.analyze(["a.js"], str(tmp_path), 30)


# ── Ruff ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ruff_analyzer_parses_json_and_maps_severity(tmp_path):
    analyzer = RuffAnalyzer()
    output = json.dumps([
        {"filename": "app.py", "code": "F821", "message": "undefined name 'x'", "location": {"row": 3, "column": 1}},
        {"filename": "app.py", "code": "E501", "message": "line too long",       "location": {"row": 10, "column": 80}},
    ])
    with patch.object(RuffAnalyzer, "_run", new=AsyncMock(return_value=(1, output, ""))):
        results = await analyzer.analyze(["app.py"], str(tmp_path), 30)

    assert len(results) == 2
    assert results[0].severity == "high"    # pyflakes (F) → real bug
    assert results[1].severity == "medium"  # style (E)


# ── Clang-Tidy ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clang_tidy_analyzer_parses_diagnostic_lines_and_skips_notes(tmp_path):
    analyzer = ClangTidyAnalyzer()
    output = (
        "src/main.cpp:12:5: warning: use of NULL where nullptr is preferred [modernize-use-nullptr]\n"
        "src/main.cpp:12:5: note: expanded from macro 'NULL'\n"
        "src/main.cpp:20:1: error: use of undeclared identifier 'foo' [clang-diagnostic-error]\n"
    )
    with patch.object(ClangTidyAnalyzer, "_run", new=AsyncMock(return_value=(1, output, ""))):
        results = await analyzer.analyze(["src/main.cpp"], str(tmp_path), 30)

    assert len(results) == 2  # the "note" line must not become its own finding
    assert results[0].rule_id  == "modernize-use-nullptr"
    assert results[0].severity == "medium"
    assert results[0].line     == 12
    assert results[0].column   == 5
    assert results[1].severity == "high"


@pytest.mark.asyncio
async def test_clang_tidy_analyzer_uses_compile_commands_when_present(tmp_path):
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "compile_commands.json").write_text("[]")
    analyzer = ClangTidyAnalyzer()
    seen_cmd = []

    async def fake_run(cmd, cwd, timeout):
        seen_cmd.extend(cmd)
        return (0, "", "")

    with patch.object(ClangTidyAnalyzer, "_run", new=AsyncMock(side_effect=fake_run)):
        await analyzer.analyze(["main.cpp"], str(tmp_path), 30)

    assert "-p" in seen_cmd
    assert seen_cmd[seen_cmd.index("-p") + 1] == str(tmp_path / "build")


@pytest.mark.asyncio
async def test_clang_tidy_analyzer_falls_back_to_extra_args_without_compile_db(tmp_path):
    analyzer = ClangTidyAnalyzer(extra_args="-- -std=c++20")
    seen_cmd = []

    async def fake_run(cmd, cwd, timeout):
        seen_cmd.extend(cmd)
        return (0, "", "")

    with patch.object(ClangTidyAnalyzer, "_run", new=AsyncMock(side_effect=fake_run)):
        await analyzer.analyze(["main.cpp"], str(tmp_path), 30)

    assert "-p" not in seen_cmd
    assert seen_cmd[-2:] == ["--", "-std=c++20"]


@pytest.mark.asyncio
async def test_clang_tidy_analyzer_raises_when_tool_missing(tmp_path):
    analyzer = ClangTidyAnalyzer()
    with patch.object(ClangTidyAnalyzer, "_run", new=AsyncMock(side_effect=ToolUnavailable("no clang-tidy"))):
        with pytest.raises(ToolUnavailable):
            await analyzer.analyze(["main.cpp"], str(tmp_path), 30)


# ── SpotBugs ───────────────────────────────────────────────────────────────────

_SPOTBUGS_XML = """<?xml version="1.0"?>
<BugCollection>
  <BugInstance type="NP_NULL_ON_SOME_PATH" priority="1">
    <LongMessage>Possible null pointer dereference in com.example.Foo.bar()</LongMessage>
    <SourceLine classname="com.example.Foo" start="12" end="14" sourcefile="Foo.java" sourcepath="com/example/Foo.java"/>
  </BugInstance>
  <BugInstance type="UNUSED_FIELD" priority="3">
    <ShortMessage>Unused field</ShortMessage>
    <SourceLine classname="com.example.Other" start="5" sourcefile="Other.java" sourcepath="com/example/Other.java"/>
  </BugInstance>
</BugCollection>"""


def _fake_spotbugs_run(xml: str):
    async def fake_run(cmd, cwd, timeout):
        report_path = cmd[cmd.index("-output") + 1]
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(xml)
        return (0, "", "")
    return fake_run


@pytest.mark.asyncio
async def test_spotbugs_analyzer_parses_xml_and_matches_changed_file(tmp_path):
    (tmp_path / "target" / "classes").mkdir(parents=True)
    java_file = "src/main/java/com/example/Foo.java"

    analyzer = SpotBugsAnalyzer()
    with patch.object(SpotBugsAnalyzer, "_run", new=AsyncMock(side_effect=_fake_spotbugs_run(_SPOTBUGS_XML))):
        results = await analyzer.analyze([java_file], str(tmp_path), 30)

    # UNUSED_FIELD is for Other.java, which isn't in the changed-files list —
    # SpotBugs is whole-program, so it must be filtered out.
    assert len(results) == 1
    assert results[0].rule_id  == "NP_NULL_ON_SOME_PATH"
    assert results[0].severity == "high"
    assert results[0].line     == 12
    assert results[0].file     == java_file


@pytest.mark.asyncio
async def test_spotbugs_analyzer_raises_without_compiled_classes(tmp_path):
    analyzer = SpotBugsAnalyzer()
    with pytest.raises(ToolUnavailable):
        await analyzer.analyze(["src/main/java/com/example/Foo.java"], str(tmp_path), 30)


@pytest.mark.asyncio
async def test_spotbugs_analyzer_checks_configured_classes_dirs_in_order(tmp_path):
    (tmp_path / "build" / "classes" / "java" / "main").mkdir(parents=True)
    analyzer = SpotBugsAnalyzer(classes_dirs="target/classes,build/classes/java/main")
    with patch.object(SpotBugsAnalyzer, "_run", new=AsyncMock(side_effect=_fake_spotbugs_run(_SPOTBUGS_XML))):
        results = await analyzer.analyze(["src/main/java/com/example/Foo.java"], str(tmp_path), 30)
    assert len(results) == 1


# ── Clippy ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clippy_analyzer_filters_to_changed_files(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = \"x\"\n")
    analyzer = ClippyAnalyzer()

    def _msg(file_name):
        return json.dumps({
            "reason": "compiler-message",
            "message": {
                "level": "warning",
                "message": "unused variable",
                "code": {"code": "unused_variables"},
                "spans": [{"file_name": file_name, "line_start": 4}],
            },
        })

    output = "\n".join([_msg("src/main.rs"), _msg("src/other.rs")])
    with patch.object(ClippyAnalyzer, "_run", new=AsyncMock(return_value=(0, output, ""))):
        results = await analyzer.analyze(["src/main.rs"], str(tmp_path), 30)

    assert len(results) == 1
    assert results[0].file == "src/main.rs"


@pytest.mark.asyncio
async def test_clippy_analyzer_raises_without_cargo_toml(tmp_path):
    analyzer = ClippyAnalyzer()
    with pytest.raises(ToolUnavailable):
        await analyzer.analyze(["src/main.rs"], str(tmp_path), 30)


# ── Roslyn ─────────────────────────────────────────────────────────────────────
#
# Empirically confirmed against a real dotnet 10 SDK install: `dotnet format
# analyzers` silently returns ZERO diagnostics when pointed directly at a bare
# .csproj (it logs "Skipping referenced project '<name>'" even though the
# exact same project reports the same warnings fine via `dotnet build`). It
# only works reliably against a solution — so every .csproj this analyzer
# touches, real or synthesized, must get wrapped in a throwaway solution
# first. These tests guard that wrapping actually happens.

def _write_format_report(report_dir: str, file_path: str) -> None:
    report = [{
        "FilePath": file_path,
        "FileChanges": [{"LineNumber": 5, "DiagnosticId": "CA1062", "FormatDescription": "Validate argument"}],
    }]
    with open(os.path.join(report_dir, "format-report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f)


@pytest.mark.asyncio
async def test_roslyn_analyzer_wraps_bare_csproj_in_throwaway_solution(tmp_path):
    """A bare .csproj (no .sln/.slnx alongside it) must be wrapped in a
    throwaway solution before being handed to `dotnet format analyzers` —
    passing the bare csproj directly silently produces zero findings."""
    (tmp_path / "App.csproj").write_text("<Project Sdk=\"Microsoft.NET.Sdk\"></Project>")
    (tmp_path / "Foo.cs").write_text("class Foo {}")
    analyzer = RoslynAnalyzer()
    seen_solution_paths = []

    async def fake_run(cmd, cwd, timeout):
        solution_path = cmd[3]
        seen_solution_paths.append(solution_path)
        report_dir = cmd[cmd.index("--report") + 1]
        _write_format_report(report_dir, os.path.join(str(tmp_path), "Foo.cs"))
        return (0, "", "")

    with patch.object(RoslynAnalyzer, "_run", new=AsyncMock(side_effect=fake_run)):
        results = await analyzer.analyze(["Foo.cs"], str(tmp_path), 30)

    assert len(results) == 1
    assert results[0].rule_id  == "CA1062"
    assert results[0].severity == "high"
    assert results[0].file     == "Foo.cs"
    assert seen_solution_paths[0].endswith((".sln", ".slnx"))


@pytest.mark.asyncio
async def test_roslyn_analyzer_uses_existing_solution_directly(tmp_path):
    """When a real .sln/.slnx already exists, use it as-is — no wrapping,
    no synthesis, and no dotnet --version call to detect a target framework."""
    (tmp_path / "App.slnx").write_text("<Solution></Solution>")
    (tmp_path / "Foo.cs").write_text("class Foo {}")
    analyzer = RoslynAnalyzer()
    seen_solution_paths = []

    async def fake_run(cmd, cwd, timeout):
        assert "--version" not in cmd
        seen_solution_paths.append(cmd[3])
        report_dir = cmd[cmd.index("--report") + 1]
        _write_format_report(report_dir, os.path.join(str(tmp_path), "Foo.cs"))
        return (0, "", "")

    with patch.object(RoslynAnalyzer, "_run", new=AsyncMock(side_effect=fake_run)):
        results = await analyzer.analyze(["Foo.cs"], str(tmp_path), 30)

    assert len(results) == 1
    assert seen_solution_paths[0] == str(tmp_path / "App.slnx")


@pytest.mark.asyncio
async def test_roslyn_analyzer_synthesizes_project_when_none_found(tmp_path):
    """No .sln/.slnx/.csproj on disk at all — e.g. only the changed file was
    fetched via the GitLab API rather than a full git clone. Roslyn should
    still run against a throwaway project (wrapped in a throwaway solution)
    built around just that file."""
    (tmp_path / "Foo.cs").write_text("class Foo {}")
    analyzer = RoslynAnalyzer()
    seen_solution_paths = []
    seen_csproj_content = []
    seen_copied_source = []

    async def fake_run(cmd, cwd, timeout):
        if "--version" in cmd:
            return (0, "10.0.200\n", "")
        # cmd = [dotnet, format, analyzers, <solution>, --verify-no-changes, --report, <dir>, --severity, info]
        # Inspect the synthesized project *now*, while its temp dir still
        # exists (it's cleaned up as soon as analyze()'s `with` block exits).
        solution_path = cmd[3]
        seen_solution_paths.append(solution_path)
        csproj_path = os.path.join(os.path.dirname(solution_path), "ReviewProject.csproj")
        with open(csproj_path, encoding="utf-8") as f:
            seen_csproj_content.append(f.read())
        seen_copied_source.append(os.path.exists(os.path.join(cwd, "Foo.cs")))

        report_dir = cmd[cmd.index("--report") + 1]
        _write_format_report(report_dir, os.path.join(cwd, "Foo.cs"))
        return (0, "", "")

    with patch.object(RoslynAnalyzer, "_run", new=AsyncMock(side_effect=fake_run)):
        results = await analyzer.analyze(["Foo.cs"], str(tmp_path), 30)

    assert len(results) == 1
    assert results[0].file == "Foo.cs"
    assert seen_solution_paths[0].endswith("ReviewProject.slnx")
    assert seen_copied_source[0] is True
    assert "<TargetFramework>net10.0</TargetFramework>" in seen_csproj_content[0]
    # Named without underscores so the synthetic project's own name doesn't
    # trip a naming-convention rule (e.g. CA1707) and show up as a fake finding.
    assert "_" not in os.path.splitext(os.path.basename(seen_solution_paths[0]))[0]


@pytest.mark.asyncio
async def test_roslyn_analyzer_raises_when_synthesis_has_nothing_to_copy(tmp_path):
    """If none of the 'changed' files actually exist on disk under repo_root,
    there's nothing to synthesize a project around — report unavailable
    rather than silently returning zero findings."""
    analyzer = RoslynAnalyzer()
    with pytest.raises(ToolUnavailable):
        await analyzer.analyze(["Missing.cs"], str(tmp_path), 30)


def test_find_project_detects_slnx():
    """dotnet new sln on newer SDKs (10+) defaults to the .slnx XML format
    instead of the classic .sln — must be detected too, and preferred over a
    bare .csproj found alongside it."""
    import tempfile as _tempfile
    from app.static_analysis.roslyn_analyzer import _find_project

    with _tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "App.csproj"), "w").close()
        open(os.path.join(d, "App.slnx"), "w").close()
        found = _find_project(d)
    assert found.endswith("App.slnx")


@pytest.mark.asyncio
async def test_roslyn_detect_tfm_matches_installed_sdk_major_version(tmp_path):
    analyzer = RoslynAnalyzer()
    with patch.object(RoslynAnalyzer, "_run", new=AsyncMock(return_value=(0, "10.0.200\n", ""))):
        tfm = await analyzer._detect_tfm(str(tmp_path), 30)
    assert tfm == "net10.0"


@pytest.mark.asyncio
async def test_roslyn_detect_tfm_falls_back_on_error(tmp_path):
    analyzer = RoslynAnalyzer()
    with patch.object(RoslynAnalyzer, "_run", new=AsyncMock(side_effect=Exception("boom"))):
        tfm = await analyzer._detect_tfm(str(tmp_path), 30)
    assert tfm == "net8.0"


# ── Runner ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_runner_dispatches_by_language_and_merges_results(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")

    runner = StaticAnalysisRunner()
    dummy = DiagnosticIssue(tool="ruff", rule_id="x", severity="medium",
                             file="app.py", line=1, column=None, message="y")
    runner._analyzers[Language.python].analyze = AsyncMock(return_value=[dummy])

    # "missing.py" doesn't exist on disk and "README.md" has no detected language —
    # both should be excluded before any analyzer runs.
    results, notes = await runner.run(["app.py", "missing.py", "README.md"], str(tmp_path))

    called_files = runner._analyzers[Language.python].analyze.call_args.args[0]
    assert called_files == ["app.py"]
    assert results == [dummy.to_review_comment()]
    assert notes == []


@pytest.mark.asyncio
async def test_runner_returns_empty_for_no_supported_files(tmp_path):
    runner = StaticAnalysisRunner()
    results, notes = await runner.run(["README.md", "LICENSE"], str(tmp_path))
    assert results == []
    assert notes == []


@pytest.mark.asyncio
async def test_runner_surfaces_note_when_tool_unavailable(tmp_path):
    """This is the exact scenario a user sees as 'static analysis issues never
    show up': the tool isn't installed. The runner must report *why*, not just
    silently return zero findings indistinguishable from a clean review."""
    (tmp_path / "app.py").write_text("x = 1\n")

    runner = StaticAnalysisRunner()
    runner._analyzers[Language.python].analyze = AsyncMock(
        side_effect=ToolUnavailable("ruff not found on PATH")
    )

    results, notes = await runner.run(["app.py"], str(tmp_path))

    assert results == []
    assert len(notes) == 1
    assert "ruff" in notes[0]


@pytest.mark.asyncio
async def test_runner_serializes_analyzers_under_max_parallel_analyzers_1(tmp_path):
    """CPU execution mode must run analyzers one at a time, not just cap
    concurrency loosely — mirrors the same self-inflicted-timeout risk this
    project already guards against for LLM requests (OLLAMA_MAX_CONCURRENCY),
    just on the static-analysis side."""
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "app.ts").write_text("const x = 1;\n")

    runner = StaticAnalysisRunner()
    in_flight = {"current": 0, "max": 0}

    async def slow_analyze(files, repo_root, timeout):
        in_flight["current"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["current"])
        await asyncio.sleep(0.02)
        in_flight["current"] -= 1
        return []

    runner._analyzers[Language.python].analyze     = AsyncMock(side_effect=slow_analyze)
    runner._analyzers[Language.typescript].analyze  = AsyncMock(side_effect=slow_analyze)

    fake_policy = ExecutionPolicy(mode="cpu", max_parallel_reviews=1,
                                   max_parallel_analyzers=1, max_parallel_llm_requests=1)
    with patch("app.static_analysis.runner.get_execution_policy", return_value=fake_policy):
        await runner.run(["app.py", "app.ts"], str(tmp_path))

    assert in_flight["max"] == 1


@pytest.mark.asyncio
async def test_runner_respects_higher_max_parallel_analyzers_in_gpu_mode(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "app.ts").write_text("const x = 1;\n")

    runner = StaticAnalysisRunner()
    in_flight = {"current": 0, "max": 0}

    async def slow_analyze(files, repo_root, timeout):
        in_flight["current"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["current"])
        await asyncio.sleep(0.02)
        in_flight["current"] -= 1
        return []

    runner._analyzers[Language.python].analyze    = AsyncMock(side_effect=slow_analyze)
    runner._analyzers[Language.typescript].analyze = AsyncMock(side_effect=slow_analyze)

    fake_policy = ExecutionPolicy(mode="gpu", max_parallel_reviews=4,
                                   max_parallel_analyzers=4, max_parallel_llm_requests=4)
    with patch("app.static_analysis.runner.get_execution_policy", return_value=fake_policy):
        await runner.run(["app.py", "app.ts"], str(tmp_path))

    assert in_flight["max"] == 2
