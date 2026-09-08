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
from app.utils.diff_parser import annotate_line_numbers, visible_new_lines
from app.utils.models import CodeFix, ReviewComment, MRContext, Severity

log = structlog.get_logger(__name__)

# Largest fix (in lines) we'll accept from the model. Bigger rewrites are
# almost always the model regenerating a whole function from memory rather
# than making a targeted change, and GitLab's one-click apply then commits
# that wholesale. The prose `suggestion` still carries the idea in that case.
MAX_FIX_LINES = 40

RESPONSE_SCHEMA = """
Respond ONLY with a valid JSON array (no markdown fences, no explanation).
Each element must follow this exact schema:
{
  "file_path":   "<string — relative path of the file>",
  "line":        <integer NEW-file line number from the diff's number column, or null for file-level>,
  "severity":    "<low|medium|high|critical>",
  "title":       "<short one-line title>",
  "description": "<detailed explanation of the issue>",
  "suggestion":  "<short prose recommendation, or null>",
  "fix": {
    "start_line":  <integer — first NEW-file line to replace>,
    "end_line":    <integer — last NEW-file line to replace (inclusive; equal to start_line for a single line)>,
    "replacement": "<the complete new source text for those lines — code only>"
  }
}

Rules for "fix":
- Provide a fix whenever you can express the correction as a direct replacement of the
  lines shown in the diff. Otherwise set "fix": null and describe it in "suggestion".
- "replacement" is the exact code that should replace lines start_line..end_line. Keep the
  original indentation, include ONLY source code — no line numbers, no +/- diff markers,
  no markdown fences, no commentary. Use "" (empty string) to delete the lines.
- The range must lie within the numbered lines shown in the diff and must include "line".
  Keep it as small as possible — replace only what actually needs to change.
- The result must be complete and compile/run as-is; don't leave placeholders like TODO or "...".
If you find no issues, return an empty array: []
"""

DIFF_LEGEND = """
Each code line below is prefixed with its line number in the NEW version of the file,
then a marker: "+" added by this change, "-" removed (no new line number), " " unchanged context.
Use those numbers for "line", "fix.start_line" and "fix.end_line".
""".strip()


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
{DIFF_LEGEND}
--- DIFF START ---
{annotate_line_numbers(diff_chunk)}
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
            # Lines the model was actually shown, so fixes can't stray outside
            # them. Empty (no @@ hunk headers → no line numbers at all) means
            # "unknown", not "nothing" — skip the containment check then.
            visible  = visible_new_lines(diff_chunk.splitlines()) or None
            comments = self._parse(raw, file_path, visible_lines=visible)
            log.info("agent.done", agent=self.name, mr=ctx.mr_iid, findings=len(comments))
            return comments
        except Exception as exc:
            log.error("agent.error", agent=self.name, error=str(exc))
            if on_error:
                on_error(exc)
            return []

    def _parse(
        self,
        raw: str,
        file_path: str | None = None,
        visible_lines: set[int] | None = None,
    ) -> list[ReviewComment]:
        """
        Turn the model's JSON array into ReviewComments. `visible_lines` is the
        set of new-file line numbers the model was actually shown for this
        file; when given, any proposed fix reaching outside it is dropped (the
        comment itself is kept) — see _parse_fix.
        """
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
                line = item.get("line")
                fix  = self._parse_fix(item.get("fix"), line, visible_lines)
                if fix is not None and line is None:
                    # A fix is inherently line-anchored; a "file-level" comment
                    # carrying one can only be posted inline (and applied) if
                    # we anchor it — the fix's first line is the natural spot.
                    line = fix.start_line
                results.append(ReviewComment(
                    agent=self.name,
                    severity=Severity(item.get("severity", "low")),
                    # Each review() call is already scoped to a single file's diff,
                    # so the caller-supplied file_path is ground truth — prefer it
                    # over the LLM's guess (which is sometimes missing or wrong).
                    file_path=file_path or item.get("file_path") or "unknown",
                    line=line,
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    suggestion=item.get("suggestion"),
                    fix=fix,
                ))
            except Exception as exc:
                log.warning("agent.parse.item_skip", error=str(exc))
        return results

    def _parse_fix(
        self,
        raw_fix,
        line: int | None,
        visible_lines: set[int] | None,
    ) -> CodeFix | None:
        """
        Validate the model's "fix" object into a CodeFix, or return None
        (keeping the finding but without a one-click fix) when it can't be
        trusted: malformed, too large, not covering the reported line, or
        reaching lines the model was never shown. A wrong fix that the MR
        author applies with one click is far worse than no fix at all, so
        every check here errs toward dropping.
        """
        if not isinstance(raw_fix, dict):
            return None
        try:
            fix = CodeFix(
                start_line=int(raw_fix["start_line"]),
                end_line=int(raw_fix["end_line"]),
                # Models routinely close the replacement with a newline; keeping
                # it would add a stray blank line to the applied suggestion.
                replacement=str(raw_fix.get("replacement") or "").rstrip("\r\n"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("agent.parse.fix_skip", agent=self.name, reason="malformed", error=str(exc))
            return None

        if fix.line_count > MAX_FIX_LINES:
            log.warning("agent.parse.fix_skip", agent=self.name, reason="too_large", lines=fix.line_count)
            return None
        if line is not None and not fix.covers(line):
            log.warning("agent.parse.fix_skip", agent=self.name, reason="range_excludes_line",
                        line=line, start=fix.start_line, end=fix.end_line)
            return None
        if visible_lines is not None:
            wanted = set(range(fix.start_line, fix.end_line + 1))
            if not wanted <= visible_lines:
                log.warning("agent.parse.fix_skip", agent=self.name, reason="outside_diff",
                            start=fix.start_line, end=fix.end_line)
                return None
        return fix
