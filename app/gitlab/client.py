"""
app/gitlab/client.py — Async GitLab API client.
Fetches MR diffs and posts review comments (inline + summary) back to GitLab.
"""

from __future__ import annotations
import hmac
import structlog
import httpx

from app.settings import get_settings
from app.utils.models import ReviewComment, ReviewResult, Severity

log = structlog.get_logger(__name__)

_EMOJI = {
    Severity.critical: "🔴",
    Severity.high:     "🟠",
    Severity.medium:   "🟡",
    Severity.low:      "🔵",
}


class GitLabClient:

    def __init__(self):
        cfg = get_settings()
        self.base_url = cfg.gitlab_url.rstrip("/") + "/api/v4"
        self.headers  = {
            "PRIVATE-TOKEN": cfg.gitlab_token,
            "Content-Type":  "application/json",
        }

    # ── Fetch ─────────────────────────────────────────────────────────────

    async def get_mr_details(self, project_id: str | int, mr_iid: int) -> dict:
        url = f"{self.base_url}/projects/{_enc(project_id)}/merge_requests/{mr_iid}"
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url, headers=self.headers)
            r.raise_for_status()
        return r.json()

    async def get_mr_diff(self, project_id: str | int, mr_iid: int) -> str:
        """Return the unified diff for an MR (all changes, all files)."""
        url = f"{self.base_url}/projects/{_enc(project_id)}/merge_requests/{mr_iid}/changes"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=self.headers)
            r.raise_for_status()

        changes = r.json().get("changes", [])
        chunks = []
        for c in changes:
            path = c.get("new_path") or c.get("old_path", "unknown")
            diff = c.get("diff", "")
            chunks.append(f"diff --git a/{path} b/{path}\n{diff}")
        return "\n".join(chunks)

    # ── Post ──────────────────────────────────────────────────────────────

    async def post_mr_note(self, project_id: str | int, mr_iid: int, body: str) -> None:
        """Post a top-level (general) comment on the MR."""
        url = f"{self.base_url}/projects/{_enc(project_id)}/merge_requests/{mr_iid}/notes"
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(url, headers=self.headers, json={"body": body})
            r.raise_for_status()
        log.info("gitlab.note.posted", mr=mr_iid)

    async def post_inline_comment(
        self,
        project_id: str | int,
        mr_iid:     int,
        comment:    ReviewComment,
        base_sha:   str,
        head_sha:   str,
        start_sha:  str,
        old_path:   str | None = None,
    ) -> bool:
        """
        Post an inline diff comment on a specific file+line.
        Returns True on success, False if GitLab rejects the position
        (e.g. line no longer exists in the diff).
        """
        if not comment.line:
            return False

        body = (
            f"{_EMOJI[comment.severity]} **[{comment.severity.upper()}] {comment.title}**\n"
            f"*Agent: {comment.agent}*\n\n"
            f"{comment.description}"
        )
        if comment.suggestion:
            body += f"\n\n**Suggestion:**\n```\n{comment.suggestion}\n```"

        payload = {
            "body": body,
            "position": {
                "base_sha":      base_sha,
                "head_sha":      head_sha,
                "start_sha":     start_sha,
                "position_type": "text",
                "old_path":      old_path or comment.file_path,
                "new_path":      comment.file_path,
                "new_line":      comment.line,
            },
        }
        url = (
            f"{self.base_url}/projects/{_enc(project_id)}"
            f"/merge_requests/{mr_iid}/discussions"
        )
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(url, headers=self.headers, json=payload)
                r.raise_for_status()
            log.debug("gitlab.inline.posted", file=comment.file_path, line=comment.line)
            return True
        except httpx.HTTPStatusError as exc:
            # 400/422 = line not in diff — fall back gracefully
            log.warning("gitlab.inline.failed",
                        status=exc.response.status_code,
                        file=comment.file_path, line=comment.line)
            return False

    # ── High-level ────────────────────────────────────────────────────────

    async def post_review(
        self,
        result: ReviewResult,
        mr_details: dict,
        path_map: dict[str, str] | None = None,
    ) -> None:
        cfg       = get_settings()
        diff_refs = mr_details.get("diff_refs") or {}
        base_sha  = diff_refs.get("base_sha",  "")
        head_sha  = diff_refs.get("head_sha",  "")
        start_sha = diff_refs.get("start_sha", "")
        path_map  = path_map or {}

        failed_inline: list[ReviewComment] = []

        # 1. Inline comments
        if cfg.post_inline_comments:
            for comment in result.comments:
                if comment.line:
                    ok = await self.post_inline_comment(
                        result.project_id, result.mr_iid,
                        comment, base_sha, head_sha, start_sha,
                        old_path=path_map.get(comment.file_path),
                    )
                    if not ok:
                        failed_inline.append(comment)

        # 2. Summary comment (always includes anything that couldn't go inline)
        if cfg.post_summary_comment:
            body = _build_summary(result, failed_inline)
            await self.post_mr_note(result.project_id, result.mr_iid, body)

    # ── Webhook verification ──────────────────────────────────────────────

    @staticmethod
    def verify_webhook(token_header: str) -> bool:
        secret = get_settings().gitlab_webhook_secret
        if not secret:
            return True
        return hmac.compare_digest(secret, token_header)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _enc(project_id: str | int) -> str:
    """URL-encode the project path (e.g. 'group/repo' → 'group%2Frepo')."""
    return str(project_id).replace("/", "%2F")


def _build_summary(result: ReviewResult, failed_inline: list[ReviewComment]) -> str:
    header = (
        f"## 🤖 Automated Code Review\n\n"
        f"{result.summary}\n\n"
        f"---\n"
        f"**Issues:** "
        f"🔴 {result.critical_count} critical · "
        f"🟠 {result.high_count} high · "
        f"🟡 {result.medium_count} medium · "
        f"🔵 {result.low_count} low\n"
    )

    if not result.comments:
        return header

    # Group by agent
    by_agent: dict[str, list[ReviewComment]] = {}
    for c in result.comments:
        by_agent.setdefault(c.agent, []).append(c)

    sections = []
    for agent, comments in by_agent.items():
        rows = "\n".join(
            f"| {_EMOJI[c.severity]} `{c.severity}` | `{c.file_path}:{c.line or '-'}` | {c.title} |"
            for c in comments
        )
        sections.append(
            f"\n### {agent.replace('_', ' ').title()}\n\n"
            f"| Severity | Location | Issue |\n"
            f"|----------|----------|-------|\n"
            f"{rows}"
        )

    # Items that couldn't be posted inline
    if failed_inline:
        rows = "\n".join(
            f"| {_EMOJI[c.severity]} `{c.severity}` | `{c.file_path}:{c.line}` | {c.title} | {c.description} |"
            for c in failed_inline
        )
        sections.append(
            f"\n### ⚠️ Could not post inline (line no longer in diff)\n\n"
            f"| Severity | Location | Issue | Detail |\n"
            f"|----------|----------|-------|--------|\n"
            f"{rows}"
        )

    footer = "\n\n---\n*Powered by GitHub Copilot (Claude Sonnet) · Code Review Agent*"
    return header + "".join(sections) + footer


# Singleton
_client: GitLabClient | None = None


def get_gitlab_client() -> GitLabClient:
    global _client
    if _client is None:
        _client = GitLabClient()
    return _client
