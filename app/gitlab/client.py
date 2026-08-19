"""
app/gitlab/client.py — Async GitLab API client.
Fetches MR diffs and posts review comments (inline + summary) back to GitLab.
"""

from __future__ import annotations
import asyncio
import hmac
import os
import structlog
import httpx
from urllib.parse import quote, urlsplit, urlunsplit

from app.settings import get_settings
from app.llm.factory import get_active_model_label
from app.utils.models import ReviewComment, ReviewResult, Severity

log = structlog.get_logger(__name__)


class UnknownGitLabInstance(Exception):
    """Raised when an instance key isn't in GITLAB_INSTANCES (settings.py)."""
    def __init__(self, instance: str):
        super().__init__(f"unknown GitLab instance {instance!r} — check GITLAB_INSTANCES in .env")
        self.instance = instance

# Marker prefix for the transient "review in progress" note (see
# post_progress_note/delete_stale_progress_notes) — a shared constant so the
# text posted and the text matched during cleanup can't drift apart.
PROGRESS_NOTE_MARKER = "🔄 **Automated code review in progress…**"

_EMOJI = {
    Severity.critical: "🔴",
    Severity.high:     "🟠",
    Severity.medium:   "🟡",
    Severity.low:      "🔵",
}


class GitLabClient:

    def __init__(self, instance: str):
        cfg = get_settings()
        inst = cfg.gitlab_instances.get(instance)
        if inst is None:
            raise UnknownGitLabInstance(instance)
        self.instance = instance
        self.base_url = inst.url.rstrip("/") + "/api/v4"
        self.headers  = {
            "PRIVATE-TOKEN": inst.token,
            "Content-Type":  "application/json",
        }
        self._token = inst.token
        self._webhook_secret = inst.webhook_secret

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

    async def get_project(self, project_id: str | int) -> dict:
        url = f"{self.base_url}/projects/{_enc(project_id)}"
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url, headers=self.headers)
            r.raise_for_status()
        return r.json()

    async def clone_mr_source(self, project_id: str | int, source_branch: str, dest_dir: str) -> tuple[bool, str]:
        """
        Shallow-clone the MR's source branch into dest_dir so static analyzers
        (Roslyn/ESLint/Ruff/Clippy/Clang-Tidy/SpotBugs) have real files to run against.
        Returns (False, reason) on any failure — callers should degrade
        gracefully to LLM-only review rather than fail the whole request, and
        `reason` is specific enough to surface directly to the user (e.g. in
        the MR summary comment) so a broken clone isn't just silently invisible.
        """
        try:
            project = await self.get_project(project_id)
        except Exception as exc:
            reason = f"could not look up project {project_id}: {exc}"
            log.warning("gitlab.clone.project_lookup_failed", project=project_id, error=str(exc))
            return False, reason

        clone_url = project.get("http_url_to_repo")
        if not clone_url:
            reason = f"project {project_id} has no http_url_to_repo"
            log.warning("gitlab.clone.no_url", project=project_id)
            return False, reason

        authed_url = _with_token(clone_url, self._token)
        cmd = [
            "git", "clone", "--depth", "1", "--branch", source_branch,
            "--single-branch", authed_url, dest_dir,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        except FileNotFoundError as exc:
            reason = f"git is not installed or not on PATH ({exc})"
            log.warning("gitlab.clone.failed", project=project_id, branch=source_branch, error=str(exc))
            return False, reason
        except asyncio.TimeoutError:
            reason = "git clone timed out after 60s"
            log.warning("gitlab.clone.failed", project=project_id, branch=source_branch, error=reason)
            return False, reason

        if proc.returncode != 0:
            detail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or ["unknown error"]
            reason = f"git clone failed: {detail[0][:200]}"
            log.warning("gitlab.clone.failed", project=project_id, branch=source_branch, error=reason)
            return False, reason

        log.info("gitlab.clone.done", project=project_id, branch=source_branch)
        return True, ""

    async def fetch_changed_files(
        self, project_id: str | int, changed_files: list[str], ref: str, dest_dir: str,
    ) -> tuple[bool, str]:
        """
        Fetch just the changed files' raw content at `ref` via the GitLab API
        and write them into dest_dir, preserving relative paths. This is the
        fallback when `clone_mr_source` isn't available or fails — no `git`
        binary needed, no full-repo download, works directly off the MR diff.
        Trade-off: tools needing whole-project context (Roslyn, Clippy,
        SpotBugs) won't have their dependencies resolved and will report
        themselves unavailable; per-file tools (ESLint, Ruff, Clang-Tidy) work fine.
        """
        fetched = 0
        errors: list[str] = []

        async with httpx.AsyncClient(timeout=20) as client:
            for path in changed_files:
                url = f"{self.base_url}/projects/{_enc(project_id)}/repository/files/{quote(path, safe='')}/raw"
                try:
                    r = await client.get(url, headers=self.headers, params={"ref": ref})
                    if r.status_code == 404:
                        continue  # file deleted/renamed in this MR — nothing to fetch
                    r.raise_for_status()
                except Exception as exc:
                    errors.append(f"{path}: {exc}")
                    continue

                dest_path = os.path.join(dest_dir, path)
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                with open(dest_path, "wb") as f:
                    f.write(r.content)
                fetched += 1

        if fetched == 0:
            detail = f" ({'; '.join(errors[:3])})" if errors else ""
            return False, f"could not fetch any changed files via the API{detail}"

        log.info("gitlab.fetch_changed_files.done", project=project_id,
                  fetched=fetched, failed=len(errors))
        return True, ""

    # ── Post ──────────────────────────────────────────────────────────────

    async def post_mr_note(self, project_id: str | int, mr_iid: int, body: str) -> int | None:
        """Post a top-level (general) comment on the MR. Returns the new note's id
        (needed to later delete a transient note, e.g. a "review in progress" placeholder)."""
        url = f"{self.base_url}/projects/{_enc(project_id)}/merge_requests/{mr_iid}/notes"
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(url, headers=self.headers, json={"body": body})
            r.raise_for_status()
        note_id = r.json().get("id")
        log.info("gitlab.note.posted", mr=mr_iid, note_id=note_id)
        return note_id

    async def delete_mr_note(self, project_id: str | int, mr_iid: int, note_id: int) -> None:
        """Delete a previously posted MR note (e.g. a "review in progress" placeholder
        once the real results are ready to post)."""
        url = f"{self.base_url}/projects/{_enc(project_id)}/merge_requests/{mr_iid}/notes/{note_id}"
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.delete(url, headers=self.headers)
            r.raise_for_status()
        log.info("gitlab.note.deleted", mr=mr_iid, note_id=note_id)

    async def post_progress_note(self, project_id: str | int, mr_iid: int) -> int | None:
        """Post the transient "review in progress" placeholder — always the
        same marked body, so delete_stale_progress_notes() can reliably find
        and clean up one left behind by a crashed/killed previous run."""
        return await self.post_mr_note(
            project_id, mr_iid,
            f"{PROGRESS_NOTE_MARKER}\n\nThis comment will be replaced once the review completes.",
        )

    async def list_mr_notes(self, project_id: str | int, mr_iid: int) -> list[dict]:
        url = f"{self.base_url}/projects/{_enc(project_id)}/merge_requests/{mr_iid}/notes"
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url, headers=self.headers, params={"per_page": 100})
            r.raise_for_status()
        return r.json()

    async def delete_stale_progress_notes(self, project_id: str | int, mr_iid: int) -> int:
        """
        Find and delete any leftover "review in progress" note on this MR —
        recovery for when a previous run posted one and then never got to
        delete it (process crashed/restarted mid-review, so its own
        try/finally cleanup never ran). Safe to call even when there's no
        stale note: returns 0 and does nothing.
        """
        try:
            notes = await self.list_mr_notes(project_id, mr_iid)
        except Exception as exc:
            log.warning("gitlab.stale_progress_notes.list_failed", mr=mr_iid, error=str(exc))
            return 0

        stale = [n for n in notes if str(n.get("body", "")).startswith(PROGRESS_NOTE_MARKER)]
        deleted = 0
        for note in stale:
            try:
                await self.delete_mr_note(project_id, mr_iid, note["id"])
                deleted += 1
            except Exception as exc:
                log.warning("gitlab.stale_progress_notes.delete_failed",
                            mr=mr_iid, note_id=note.get("id"), error=str(exc))
        if deleted:
            log.info("gitlab.stale_progress_notes.cleaned", mr=mr_iid, count=deleted)
        return deleted

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

    def verify_webhook(self, token_header: str) -> bool:
        if not self._webhook_secret:
            return True
        return hmac.compare_digest(self._webhook_secret, token_header)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _enc(project_id: str | int) -> str:
    """URL-encode the project path (e.g. 'group/repo' → 'group%2Frepo')."""
    return str(project_id).replace("/", "%2F")


def _with_token(clone_url: str, token: str) -> str:
    """Inject the access token into an https clone URL for non-interactive auth."""
    parts = urlsplit(clone_url)
    netloc = f"oauth2:{token}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


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

    sections = []

    if result.static_analysis_notes:
        notes = "\n".join(f"- {n}" for n in result.static_analysis_notes)
        sections.append(f"\n### ⚙️ Static Analysis\n\n{notes}")

    if result.comments:
        # Group by agent
        by_agent: dict[str, list[ReviewComment]] = {}
        for c in result.comments:
            by_agent.setdefault(c.agent, []).append(c)

        for agent, comments in by_agent.items():
            rows = "\n".join(
                f"| {_EMOJI[c.severity]} `{c.severity.value}` | `{c.file_path}:{c.line or '-'}` | {c.title} |"
                for c in comments
            )
            sections.append(
                f"\n### {agent.replace('_', ' ').replace(':', ' · ').title()}\n\n"
                f"| Severity | Location | Issue |\n"
                f"|----------|----------|-------|\n"
                f"{rows}"
            )

        # Items that couldn't be posted inline
        if failed_inline:
            rows = "\n".join(
                f"| {_EMOJI[c.severity]} `{c.severity.value}` | `{c.file_path}:{c.line}` | {c.title} | {c.description} |"
                for c in failed_inline
            )
            sections.append(
                f"\n### ⚠️ Could not post inline (line no longer in diff)\n\n"
                f"| Severity | Location | Issue | Detail |\n"
                f"|----------|----------|-------|--------|\n"
                f"{rows}"
            )

    footer = f"\n\n---\n*Powered by {get_active_model_label()} · Code Review Agent*"
    return header + "".join(sections) + footer


# One client per configured GitLab instance, built lazily and cached.
_clients: dict[str, GitLabClient] = {}


def get_gitlab_client(instance: str) -> GitLabClient:
    """Raises UnknownGitLabInstance if `instance` isn't in GITLAB_INSTANCES."""
    client = _clients.get(instance)
    if client is None:
        client = GitLabClient(instance)
        _clients[instance] = client
    return client
