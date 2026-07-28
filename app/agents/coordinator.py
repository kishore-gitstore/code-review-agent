"""
app/agents/coordinator.py — Runs all specialist agents concurrently,
deduplicates findings, generates a summary, returns ReviewResult.
"""

from __future__ import annotations
import asyncio
import structlog
from typing import Callable, Optional

from app.settings import get_settings
from app.llm.factory import get_llm_client
from app.utils.models import MRContext, ReviewComment, ReviewResult, Severity
from app.utils.diff_parser import parse_diff

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
        self._min_sev        = Severity(cfg.min_severity_to_comment)
        self._max_diff_lines = cfg.max_diff_lines

    @property
    def agents(self):
        return self._agents

    async def run(
        self,
        ctx: MRContext,
        *,
        on_agent_start: Optional[Callable[[str], None]] = None,
        on_agent_done:  Optional[Callable[[str, int], None]] = None,
    ) -> ReviewResult:
        log.info("coordinator.start", mr=ctx.mr_iid, agents=[a.name for a in self._agents])

        file_diffs = parse_diff(ctx.diff, max_lines=self._max_diff_lines)
        if not file_diffs:
            log.warning("coordinator.no_diff", mr=ctx.mr_iid)

        # Run each agent across ALL file diffs as a single logical unit so that
        # progress callbacks fire once per agent (not once per file-diff).
        async def _run_agent(agent) -> list[ReviewComment]:
            if on_agent_start:
                on_agent_start(agent.name)
            per_file = await asyncio.gather(
                *[agent.review(ctx, fd.full_diff) for fd in file_diffs]
            )
            comments = [c for batch in per_file for c in batch]
            if on_agent_done:
                on_agent_done(agent.name, len(comments))
            return comments

        nested: list[list[ReviewComment]] = await asyncio.gather(
            *[_run_agent(agent) for agent in self._agents]
        )
        all_comments = [c for sub in nested for c in sub]

        filtered = self._filter(all_comments)
        deduped  = self._dedup(filtered)
        sorted_  = sorted(deduped, key=lambda c: _SEV_ORDER[c.severity])
        summary  = await self._summarise(ctx, sorted_)

        result = ReviewResult(
            mr_iid=ctx.mr_iid,
            project_id=ctx.project_id,
            summary=summary,
            comments=sorted_,
        )
        result.compute_counts()
        log.info("coordinator.done", mr=ctx.mr_iid, total=result.total_issues,
                 critical=result.critical_count, high=result.high_count)
        return result

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

    async def _summarise(self, ctx: MRContext, comments: list[ReviewComment]) -> str:
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

        try:
            return await get_llm_client().generate(system, user)
        except Exception as exc:
            log.error("coordinator.summary.error", error=str(exc))
            counts = {s: sum(1 for c in comments if c.severity == s) for s in Severity}
            return (
                f"🔍 Found **{len(comments)} issue(s)**: "
                f"{counts[Severity.critical]} critical, {counts[Severity.high]} high, "
                f"{counts[Severity.medium]} medium, {counts[Severity.low]} low."
            )
