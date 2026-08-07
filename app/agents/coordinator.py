"""
app/agents/coordinator.py — Runs all specialist agents concurrently,
deduplicates findings, generates a summary, returns ReviewResult.
"""

from __future__ import annotations
import asyncio
import time
import structlog
from datetime import datetime
from typing import Callable, Optional

from app.execution.policy import get_execution_policy
from app.settings import get_settings
from app.llm.factory import get_llm_client
from app.utils.models import MRContext, ReviewComment, ReviewResult, Severity
from app.utils.diff_parser import parse_diff
from app.static_analysis import StaticAnalysisRunner

from .code_quality_agent   import CodeQualityAgent
from .security_agent       import SecurityAgent
from .performance_agent    import PerformanceAgent
from .documentation_agent  import DocumentationAgent

log = structlog.get_logger(__name__)

_SEV_ORDER = {
    Severity.critical: 0,
    Severity.high:     1,
    Severity.medium:   2,
    Severity.low:      3,
}


class CoordinatorAgent:

    def __init__(self):
        cfg = get_settings()
        self._agents = []
        if cfg.enable_code_quality:   self._agents.append(CodeQualityAgent())
        if cfg.enable_security:       self._agents.append(SecurityAgent())
        if cfg.enable_performance:    self._agents.append(PerformanceAgent())
        if cfg.enable_documentation:  self._agents.append(DocumentationAgent())
        self._min_sev          = Severity(cfg.min_severity_to_comment)
        self._max_diff_lines   = cfg.max_diff_lines
        self._static_runner    = StaticAnalysisRunner() if cfg.enable_static_analysis else None
        # Sized from the resolved execution policy (EXECUTION_MODE=cpu forces
        # this to 1 regardless of MAX_PARALLEL_LLM_REQUESTS) — see
        # app/execution/policy.py.
        self._agent_concurrency = max(1, get_execution_policy().max_parallel_llm_requests)

    @property
    def agents(self):
        return self._agents

    async def run(
        self,
        ctx: MRContext,
        *,
        on_agent_start:    Optional[Callable[[str], None]] = None,
        on_agent_done:     Optional[Callable[[str, int], None]] = None,
        on_llm_request:    Optional[Callable[[str, str, datetime, datetime, bool], None]] = None,
        on_analyzer_done:  Optional[Callable[[str, str, int, int, int], None]] = None,
        on_phase:          Optional[Callable[[str, int], None]] = None,
    ) -> ReviewResult:
        started = time.monotonic()
        log.info("coordinator.start", mr=ctx.mr_iid, agents=[a.name for a in self._agents],
                 agent_concurrency=self._agent_concurrency)

        file_diffs = parse_diff(ctx.diff, max_lines=self._max_diff_lines)
        if not file_diffs:
            log.warning("coordinator.no_diff", mr=ctx.mr_iid)

        changed_line_map: dict[str, set[int]] = {fd.path: fd.changed_lines for fd in file_diffs}

        # ── Static analysis stage: Language Detector -> registered analyzer plugin(s) ──
        phase_started = time.monotonic()
        static_comments: list[ReviewComment] = []
        static_notes: list[str] = []
        if self._static_runner:
            if ctx.repo_root:
                static_comments, static_notes = await self._static_runner.run(
                    ctx.changed_files, ctx.repo_root, on_analyzer_done=on_analyzer_done
                )
                static_comments, dropped = self._scope_to_diff(static_comments, changed_line_map)
                log.info("coordinator.static_analysis.done", mr=ctx.mr_iid,
                         findings=len(static_comments), dropped_outside_diff=dropped, notes=static_notes)
            else:
                static_notes = [ctx.repo_root_note or "static analysis skipped — no local checkout available"]
                log.info("coordinator.static_analysis.skipped", mr=ctx.mr_iid, reason=static_notes[0])
        if on_phase:
            on_phase("static_analysis", int((time.monotonic() - phase_started) * 1000))

        static_by_file: dict[str, list[ReviewComment]] = {}
        for c in static_comments:
            static_by_file.setdefault(c.file_path, []).append(c)

        # One semaphore shared across every agent x file-diff review() call, so
        # a cpu-mode execution policy (max_parallel_llm_requests=1) gives
        # genuinely sequential end-to-end execution — not just "4 agents
        # fired at once but queued somewhere downstream".
        review_gate = asyncio.Semaphore(self._agent_concurrency)

        async def _review_one(agent, fd) -> list[ReviewComment]:
            async with review_gate:
                req_started = datetime.now()
                failed = False

                def _mark_failed(_exc: Exception) -> None:
                    nonlocal failed
                    failed = True

                comments = await agent.review(
                    ctx, fd.full_diff, static_by_file.get(fd.path, []),
                    file_path=fd.path, on_error=_mark_failed,
                )
                if on_llm_request:
                    on_llm_request(agent.name, fd.path, req_started, datetime.now(), not failed)
                return comments

        # Run each agent across ALL file diffs as a single logical unit so that
        # progress callbacks fire once per agent (not once per file-diff). Static
        # findings for a file are injected into that file's LLM prompt so the
        # model can reason jointly instead of re-reporting the same issue.
        async def _run_agent(agent) -> list[ReviewComment]:
            if on_agent_start:
                on_agent_start(agent.name)
            per_file = await asyncio.gather(
                *[_review_one(agent, fd) for fd in file_diffs]
            )
            comments = [c for batch in per_file for c in batch]
            if on_agent_done:
                on_agent_done(agent.name, len(comments))
            return comments

        phase_started = time.monotonic()
        if self._agent_concurrency <= 1:
            # Dispatch agents strictly one at a time — on_agent_start for the
            # next agent doesn't fire until the previous one has fully
            # finished. asyncio.gather below would still fire every
            # on_agent_start immediately (only the LLM call itself is gated
            # by review_gate), which isn't "one at a time" from the caller's
            # point of view — e.g. progress logs/UI would show all agents as
            # started at once even though their requests queue internally.
            nested: list[list[ReviewComment]] = []
            for agent in self._agents:
                nested.append(await _run_agent(agent))
        else:
            nested = await asyncio.gather(
                *[_run_agent(agent) for agent in self._agents]
            )
        ai_comments = [c for sub in nested for c in sub]
        if on_phase:
            on_phase("llm_review", int((time.monotonic() - phase_started) * 1000))

        # ── Merge Results: static analysis findings + AI review ──
        phase_started = time.monotonic()
        all_comments = static_comments + ai_comments

        filtered = self._filter(all_comments)
        deduped  = self._dedup(filtered)
        sorted_  = sorted(deduped, key=lambda c: _SEV_ORDER[c.severity])
        if on_phase:
            on_phase("merge", int((time.monotonic() - phase_started) * 1000))

        summary = await self._summarise(ctx, sorted_, on_llm_request)

        result = ReviewResult(
            mr_iid=ctx.mr_iid,
            project_id=ctx.project_id,
            summary=summary,
            comments=sorted_,
            static_analysis_notes=static_notes,
        )
        result.compute_counts()
        log.info("coordinator.done", mr=ctx.mr_iid, total=result.total_issues,
                 critical=result.critical_count, high=result.high_count,
                 duration_ms=int((time.monotonic() - started) * 1000))
        return result

    @staticmethod
    def _scope_to_diff(
        comments: list[ReviewComment], changed_line_map: dict[str, set[int]],
    ) -> tuple[list[ReviewComment], int]:
        """
        Keep only findings on lines this diff actually added (or file-level
        findings with no line number). Static analyzers run against the whole
        current file on disk, not just the diff, so without this a review
        would surface pre-existing issues on untouched lines as if this MR
        introduced them. Returns (kept, dropped_count) for logging.
        """
        kept = []
        dropped = 0
        for c in comments:
            if c.line is None or c.line in changed_line_map.get(c.file_path, set()):
                kept.append(c)
            else:
                dropped += 1
        return kept, dropped

    def _filter(self, comments: list[ReviewComment]) -> list[ReviewComment]:
        threshold = _SEV_ORDER[self._min_sev]
        return [c for c in comments if _SEV_ORDER[c.severity] <= threshold]

    def _dedup(self, comments: list[ReviewComment]) -> list[ReviewComment]:
        seen: dict[tuple, ReviewComment] = {}
        for c in comments:
            key = (c.file_path, c.line, c.title.lower()[:50])
            if key not in seen or _SEV_ORDER[c.severity] < _SEV_ORDER[seen[key].severity]:
                seen[key] = c
        return list(seen.values())

    async def _summarise(
        self,
        ctx: MRContext,
        comments: list[ReviewComment],
        on_llm_request: Optional[Callable[[str, str, datetime, datetime, bool], None]] = None,
    ) -> str:
        if not comments:
            return "✅ **No significant issues found.** This MR looks good to merge."

        findings = "\n".join(
            f"- [{c.severity.upper()}] {c.agent}: {c.title} ({c.file_path}:{c.line or 'file'})"
            for c in comments[:30]
        )
        system = (
            "You are a senior engineer writing a concise GitLab MR review summary. "
            "Use markdown. Be direct and actionable. "
            "Start with a verdict: ✅ Approve / ⚠️ Needs Discussion / 🔴 Request Changes."
        )
        user = f"""
MR: {ctx.title}
Branch: {ctx.source_branch} → {ctx.target_branch}
Author: {ctx.author}

Automated findings:
{findings}

Write a 3–5 sentence summary covering:
1. Overall verdict
2. Most critical issues to fix
3. Any positives
4. Recommended next steps
""".strip()

        started = datetime.now()
        succeeded = True
        try:
            return await get_llm_client().generate(system, user)
        except Exception as exc:
            succeeded = False
            log.error("coordinator.summary.error", error=str(exc))
            counts = {s: sum(1 for c in comments if c.severity == s) for s in Severity}
            return (
                f"🔍 Found **{len(comments)} issue(s)**: "
                f"{counts[Severity.critical]} critical, {counts[Severity.high]} high, "
                f"{counts[Severity.medium]} medium, {counts[Severity.low]} low."
            )
        finally:
            if on_llm_request:
                on_llm_request("summary", "-", started, datetime.now(), succeeded)
