"""
app/static_analysis/runner.py — Orchestrates the static analysis pipeline stage:

    changed files -> Language Detector -> registered analyzer plugin(s)
                  -> DiagnosticIssue (per analyzer) -> ReviewComment (merged)

The engine itself (this file) has no knowledge of any specific analyzer — it
only knows the Language -> analyzer-class registry (registry.py). A missing
tool or a per-file error is swallowed and logged rather than failing the
whole review. Concurrency across language groups is capped by
EXECUTION.max_parallel_analyzers (1 on a CPU-only box — analyzers run one at
a time — higher when GPU/cloud capacity is available).
"""

from __future__ import annotations
import asyncio
import os
import time
import structlog
from typing import Callable, Optional

from app.execution.policy import get_execution_policy
from app.settings import get_settings
from app.utils.models import ReviewComment
from .base_analyzer import ToolUnavailable
from .diagnostics import DiagnosticIssue
from .language_detector import Language, group_by_language
from .registry import registered_analyzers

log = structlog.get_logger(__name__)


class StaticAnalysisRunner:

    def __init__(self):
        cfg = get_settings()
        self._timeout = cfg.static_analysis_timeout
        self._analyzers = {
            lang: analyzer_cls.from_settings(cfg)
            for lang, analyzer_cls in registered_analyzers().items()
        }

    async def run(
        self,
        changed_files: list[str],
        repo_root: str,
        on_analyzer_done: Optional[Callable[[str, str, int, int, int], None]] = None,
    ) -> tuple[list[ReviewComment], list[str]]:
        """
        Run every applicable analyzer against changed_files found under repo_root.
        Returns (findings, notes) — notes explain why a language was skipped
        (tool not installed, no project file found, etc.) so that a review with
        zero static findings can be told apart from one where analysis never ran.

        on_analyzer_done(tool_name, language, files_count, findings_count, duration_ms)
        fires once per language group that actually ran to completion (not for
        skipped/unavailable ones — those are already covered by the notes list).
        """
        groups = group_by_language(changed_files)
        if not groups:
            return [], []

        max_parallel = max(1, get_execution_policy().max_parallel_analyzers)
        gate = asyncio.Semaphore(max_parallel)
        log.info("static_analysis.start", languages=[l.value for l in groups], max_parallel_analyzers=max_parallel)

        async def _run_one(lang: Language, files: list[str]) -> tuple[list[DiagnosticIssue], list[str]]:
            analyzer = self._analyzers.get(lang)
            if not analyzer:
                return [], []
            existing = [f for f in files if os.path.exists(os.path.join(repo_root, f))]
            if not existing:
                return [], []

            queued_at = time.monotonic()
            async with gate:
                queue_wait_ms = int((time.monotonic() - queued_at) * 1000)
                log.info("static_analysis.analyzer.start", language=lang.value, tool=analyzer.tool_name,
                         files=len(existing), queue_wait_ms=queue_wait_ms)
                started = time.monotonic()
                try:
                    findings = await analyzer.analyze(existing, repo_root, self._timeout)
                    duration_ms = int((time.monotonic() - started) * 1000)
                    log.info("static_analysis.done", language=lang.value, tool=analyzer.tool_name,
                             findings=len(findings), duration_ms=duration_ms)
                    if on_analyzer_done:
                        on_analyzer_done(analyzer.tool_name, lang.value, len(existing), len(findings), duration_ms)
                    return findings, []
                except ToolUnavailable as exc:
                    note = f"{analyzer.tool_name} ({lang.value}) skipped — {exc}"
                    log.warning("static_analysis.tool_unavailable", language=lang.value,
                                tool=analyzer.tool_name, detail=str(exc),
                                duration_ms=int((time.monotonic() - started) * 1000))
                    return [], [note]
                except Exception as exc:
                    note = f"{analyzer.tool_name} ({lang.value}) failed — {exc}"
                    log.warning("static_analysis.error", language=lang.value,
                                tool=analyzer.tool_name, error=str(exc),
                                duration_ms=int((time.monotonic() - started) * 1000))
                    return [], [note]

        nested = await asyncio.gather(*[_run_one(lang, files) for lang, files in groups.items()])
        diagnostics = [d for batch, _ in nested for d in batch]
        notes = [n for _, batch_notes in nested for n in batch_notes]
        comments = [d.to_review_comment() for d in diagnostics]
        return comments, notes
