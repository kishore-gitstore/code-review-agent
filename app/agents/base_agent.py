"""
app/agents/base_agent.py — Abstract base all specialist agents inherit from.
Uses get_llm_client() which resolves to whatever LLM_PROVIDER is set in .env.
"""

from __future__ import annotations
import json
import re
import structlog
from abc import ABC, abstractmethod
from typing import Callable, Optional

from app.llm.factory import get_llm_client
from app.utils.models import ReviewComment, MRContext, Severity

log = structlog.get_logger(__name__)

RESPONSE_SCHEMA = """
Respond ONLY with a valid JSON array (no markdown fences, no explanation).
Each element must follow this exact schema:
{
  "file_path":   "<string — relative path of the file>",
  "line":        <integer line number, or null for file-level>,
  "severity":    "<low|medium|high|critical>",
  "title":       "<short one-line title>",
  "description": "<detailed explanation of the issue>",
  "suggestion":  "<concrete fix or recommendation, or null>"
}
If you find no issues, return an empty array: []
"""


class BaseAgent(ABC):
    name: str = "base"

    @property
    @abstractmethod
    def system_prompt(self) -> str: ...

    def build_user_prompt(
        self,
        ctx: MRContext,
        diff_chunk: str,
        static_findings: list[ReviewComment] | None = None,
        file_path: str | None = None,
    ) -> str:
        static_section = ""
        if static_findings:
            lines = "\n".join(
                f"- [{f.severity.upper()}] {f.file_path}:{f.line or '-'} ({f.title}): {f.description}"
                for f in static_findings
            )
            static_section = f"""

--- STATIC ANALYSIS RESULTS (from linters/compilers — already confirmed, don't re-report these, use them to focus your review elsewhere) ---
{lines}
--- END STATIC ANALYSIS RESULTS ---"""

        return f"""
Merge Request: {ctx.title}
Branch: {ctx.source_branch} → {ctx.target_branch}
Author: {ctx.author}
Description: {ctx.description or "N/A"}
File: {file_path or "unknown"}
{static_section}
--- DIFF START ---
{diff_chunk}
--- DIFF END ---

{RESPONSE_SCHEMA}
""".strip()

    async def review(
        self,
        ctx: MRContext,
        diff_chunk: str,
        static_findings: list[ReviewComment] | None = None,
        file_path: str | None = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> list[ReviewComment]:
        client = get_llm_client()
        log.info("agent.start", agent=self.name, mr=ctx.mr_iid, file=file_path)
        try:
            raw      = await client.generate(
                self.system_prompt, self.build_user_prompt(ctx, diff_chunk, static_findings, file_path)
            )
            comments = self._parse(raw, file_path)
            log.info("agent.done", agent=self.name, mr=ctx.mr_iid, findings=len(comments))
            return comments
        except Exception as exc:
            log.error("agent.error", agent=self.name, error=str(exc))
            if on_error:
                on_error(exc)
            return []

    def _parse(self, raw: str, file_path: str | None = None) -> list[ReviewComment]:
        cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
        start   = cleaned.find("[")
        end     = cleaned.rfind("]")
        if start == -1 or end == -1:
            log.warning("agent.parse.no_array", agent=self.name, raw=raw[:300])
            return []
        try:
            items = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            log.warning("agent.parse.json_error", agent=self.name, error=str(exc))
            return []

        results = []
        for item in items:
            try:
                results.append(ReviewComment(
                    agent=self.name,
                    severity=Severity(item.get("severity", "low")),
                    # Each review() call is already scoped to a single file's diff,
                    # so the caller-supplied file_path is ground truth — prefer it
                    # over the LLM's guess (which is sometimes missing or wrong).
                    file_path=file_path or item.get("file_path") or "unknown",
                    line=item.get("line"),
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    suggestion=item.get("suggestion"),
                ))
            except Exception as exc:
                log.warning("agent.parse.item_skip", error=str(exc))
        return results
