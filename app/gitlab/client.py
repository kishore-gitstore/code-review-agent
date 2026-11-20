"""
app/gitlab/client.py — Async GitLab API client.
Fetches MR diffs and posts review comments (inline + summary) back to GitLab.
"""

from __future__ import annotations
import asyncio
import hmac
import os
import re
import structlog
import httpx
from datetime import datetime
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
# post_progress_note/retire_progress_notes) — a shared constant so the
# text posted and the text matched during cleanup can't drift apart.
PROGRESS_NOTE_MARKER = "🔄 **Automated code review in progress…**"

# Markers for what a progress note turns into when the review it announced
# ends without results. Deliberately NOT strings starting with
# PROGRESS_NOTE_MARKER: retire_progress_notes() matches on that prefix, and a
# record the next run silently overwrote would defeat the point of leaving an
# audit trail on the MR.
CANCELLED_NOTE_MARKER   = "🛑 **Automated code review cancelled**"
FAILED_NOTE_MARKER      = "❌ **Automated code review failed**"
INTERRUPTED_NOTE_MARKER = "⚠️ **Automated code review interrupted**"


class ForbiddenGitLabRequest(RuntimeError):
    """A request the agent must never send — see _guard_request."""


# The complete list of things this agent may change on GitLab. Anything else
# — above all a DELETE, or a write aimed at the merge request itself — is
# refused before it leaves the process. The agent sends NO deletes at all:
# notes it no longer needs are edited into their final form instead.
_ALLOWED_WRITES = {
    "POST": re.compile(r"/merge_requests/\d+/(notes|discussions)$"),
    "PUT":  re.compile(r"/merge_requests/\d+/notes/\d+$"),
}


async def _guard_request(request: httpx.Request) -> None:
    """httpx request hook installed on every GitLab client (see _http).

    Enforced at the transport level rather than trusted to each call site, so
    a future method that builds a URL wrong — or a refactor that drops a
    /notes/{id} tail — fails loudly here instead of reaching GitLab's
    "delete/close this merge request" endpoints.
    """
    method = request.method.upper()
    if method in ("GET", "HEAD"):
        return
    path = request.url.path.rstrip("/")
    allowed = _ALLOWED_WRITES.get(method)
    if allowed is None or not allowed.search(path):
        log.error("gitlab.request.blocked", method=method, path=path)
        raise ForbiddenGitLabRequest(
            f"refusing to send {method} {path}: the review agent only adds or edits "
            "its own merge-request notes and never deletes anything"
        )


def _http(timeout: float) -> httpx.AsyncClient:
    """The only way this module talks to GitLab — every client carries the guard."""
    return httpx.AsyncClient(timeout=timeout, event_hooks={"request": [_guard_request]})

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
        async with _http(20) as c:
            r = await c.get(url, headers=self.headers)
            r.raise_for_status()
        return r.json()

    async def get_mr_diff(self, project_id: str | int, mr_iid: int) -> str:
        """Return the unified diff for an MR (all changes, all files)."""
        url = f"{self.base_url}/projects/{_enc(project_id)}/merge_requests/{mr_iid}/changes"
        async with _http(30) as client:
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
        async with _http(20) as c:
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

        async with _http(20) as client:
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
        (needed to later edit a transient note, e.g. the "review in progress"
        placeholder, into its final form)."""
        url = f"{self.base_url}/projects/{_enc(project_id)}/merge_requests/{mr_iid}/notes"
        async with _http(20) as c:
            r = await c.post(url, headers=self.headers, json={"body": body})
            r.raise_for_status()
        note_id = r.json().get("id")
        log.info("gitlab.note.posted", mr=mr_iid, note_id=note_id)
        return note_id

    async def update_mr_note(self, project_id: str | int, mr_iid: int, note_id: int, body: str) -> None:
        """Rewrite the body of ONE note this agent posted earlier.

        This is how the agent retires its "review in progress" placeholder:
        the note is edited into the summary (or a failed/cancelled record),
        never deleted — the agent sends no DELETE requests at all. Both ids
        must be real positive integers, so a missing id can't turn the URL
        into one that addresses the merge request itself; _guard_request
        enforces the same on the wire.
        """
        if not _is_positive_int(mr_iid) or not _is_positive_int(note_id):
            raise ValueError(
                f"refusing to edit: mr_iid={mr_iid!r} and note_id={note_id!r} "
                "must both be positive integers"
            )
        url = f"{self.base_url}/projects/{_enc(project_id)}/merge_requests/{int(mr_iid)}/notes/{int(note_id)}"
        async with _http(20) as c:
            r = await c.put(url, headers=self.headers, json={"body": body})
            r.raise_for_status()
        log.info("gitlab.note.updated", mr=mr_iid, note_id=note_id)

    async def update_or_post_note(self, project_id: str | int, mr_iid: int,
                                  note_id: int | None, body: str) -> None:
        """Edit `note_id` into `body`; post `body` as a new note if there is no
        note to edit, or the edit fails (e.g. someone removed the placeholder
        by hand). Either way the MR ends up carrying `body` exactly once."""
        if note_id is not None:
            try:
                await self.update_mr_note(project_id, mr_iid, note_id, body)
                return
            except Exception as exc:
                log.warning("gitlab.note.update_failed_posting_new", mr=mr_iid,
                            note_id=note_id, error=str(exc))
        await self.post_mr_note(project_id, mr_iid, body)

    async def post_progress_note(self, project_id: str | int, mr_iid: int) -> int | None:
        """Post the transient "review in progress" placeholder — always the
        same marked body, so retire_progress_notes() can reliably find one
        left behind by a crashed/killed previous run."""
        return await self.post_mr_note(
            project_id, mr_iid,
            f"{PROGRESS_NOTE_MARKER}\n\nThis comment will be updated once the review completes.",
        )

    async def announce_cancelled(
        self,
        project_id:   str | int,
        mr_iid:       int,
        cancelled_by: str = "",
        agents_done:  int | None = None,
        agents_total: int | None = None,
        duration_s:   float | None = None,
    ) -> None:
        """
        Leave the permanent record that a review was stopped mid-flight.

        The "review in progress" placeholder is found by its marker and
        edited into this record; with no placeholder on the MR (progress
        notes are off, or the cancel landed before one was posted) the record
        is posted as a new note. The wording leads with "no findings were
        posted" because the dangerous reading of a silent review is "the bot
        looked and found nothing" — a cancelled review is an absence of
        evidence, not evidence of absence.
        """
        body = cancelled_note_body(cancelled_by, agents_done, agents_total, duration_s)
        if await self.retire_progress_notes(project_id, mr_iid, body) == 0:
            await self.post_mr_note(project_id, mr_iid, body)

    async def _own_username(self) -> str:
        """Username the configured token authenticates as (cached)."""
        if getattr(self, "_own_name", None) is None:
            async with _http(20) as c:
                r = await c.get(f"{self.base_url}/user", headers=self.headers)
                r.raise_for_status()
            self._own_name = str(r.json().get("username", ""))
        return self._own_name

    async def list_mr_notes(self, project_id: str | int, mr_iid: int) -> list[dict]:
        url = f"{self.base_url}/projects/{_enc(project_id)}/merge_requests/{mr_iid}/notes"
        async with _http(20) as c:
            r = await c.get(url, headers=self.headers, params={"per_page": 100})
            r.raise_for_status()
        return r.json()

    async def retire_progress_notes(self, project_id: str | int, mr_iid: int,
                                    body: str | None = None) -> int:
        """
        Edit every "review in progress" note this agent left on the MR into
        `body` (default: an "interrupted" record). Recovery for when a previous
        run posted one and never got to finish it (process crashed/restarted
        mid-review), and the way a cancellation marks its own placeholder.
        Nothing is deleted. Safe to call when there's no such note: returns 0
        and does nothing.
        """
        body = body or interrupted_note_body()
        try:
            notes = await self.list_mr_notes(project_id, mr_iid)
        except Exception as exc:
            log.warning("gitlab.stale_progress_notes.list_failed", mr=mr_iid, error=str(exc))
            return 0

        # Only our OWN, user-visible progress placeholders. A bare prefix match
        # isn't enough: a person quoting the placeholder in a reply, or a
        # system note, would be swept up with it. Fail closed — if we can't
        # establish who the token belongs to, touch nothing.
        candidates = [
            n for n in notes
            if str(n.get("body", "")).startswith(PROGRESS_NOTE_MARKER) and not n.get("system")
        ]
        stale = []
        own = None
        if any((n.get("author") or {}).get("username") for n in candidates):
            try:
                own = await self._own_username()
            except Exception as exc:
                log.warning("gitlab.stale_progress_notes.identity_failed", mr=mr_iid, error=str(exc))
                return 0
        for n in candidates:
            author = (n.get("author") or {}).get("username")
            if author and author != own:
                log.info("gitlab.stale_progress_notes.skipped_foreign", mr=mr_iid,
                         note_id=n.get("id"), author=author)
                continue
            stale.append(n)
        retired = 0
        for note in stale:
            try:
                await self.update_mr_note(project_id, mr_iid, note["id"], body)
                retired += 1
            except Exception as exc:
                log.warning("gitlab.stale_progress_notes.update_failed",
                            mr=mr_iid, note_id=note.get("id"), error=str(exc))
        if retired:
            log.info("gitlab.stale_progress_notes.retired", mr=mr_iid, count=retired)
        return retired

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

        payload = {
            "body": _build_inline_body(comment, applicable=get_settings().post_fix_suggestions),
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
            async with _http(20) as c:
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
        progress_note_id: int | None = None,
    ) -> None:
        """Post the inline findings, then turn the "review in progress"
        placeholder (`progress_note_id`, if one was posted) into the summary.
        The placeholder is edited, not deleted and re-posted — see
        _guard_request for why this agent never deletes anything."""
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
            await self.update_or_post_note(result.project_id, result.mr_iid, progress_note_id, body)
        elif progress_note_id is not None:
            # No summary wanted, but the placeholder must not keep claiming
            # the review is still running.
            await self.update_or_post_note(
                result.project_id, result.mr_iid, progress_note_id,
                f"✅ **Automated code review complete** — {result.total_issues} finding(s), "
                "posted as inline comments.",
            )

    # ── Webhook verification ──────────────────────────────────────────────

    def verify_webhook(self, token_header: str) -> bool:
        if not self._webhook_secret:
            return True
        return hmac.compare_digest(self._webhook_secret, token_header)


# ── Note bodies ───────────────────────────────────────────────────────────────

def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def cancelled_note_body(cancelled_by: str = "", agents_done: int | None = None,
                        agents_total: int | None = None, duration_s: float | None = None) -> str:
    rows = []
    if cancelled_by:
        rows.append(("Cancelled by", cancelled_by))
    rows.append(("Cancelled at", _stamp()))
    if duration_s is not None:
        rows.append(("Ran for", f"{int(duration_s // 60)}m {int(duration_s % 60):02d}s"))
    if agents_total:
        rows.append(("Agents completed", f"{agents_done or 0} of {agents_total}"))
    table = "\n".join(f"| {label} | {value} |" for label, value in rows)
    return (
        f"{CANCELLED_NOTE_MARKER}\n\n"
        "This merge request's automated review was stopped before it finished, so "
        "**no findings were posted**. Treat this MR as *not reviewed* — not as approved.\n\n"
        "| | |\n|---|---|\n"
        f"{table}\n\n"
        "Push a new commit to this branch, or re-trigger the review, to run it again."
    )


def failed_note_body() -> str:
    # The exception text stays in the service log and the dashboard report:
    # it can carry internal hostnames and paths that don't belong on an MR.
    return (
        f"{FAILED_NOTE_MARKER}\n\n"
        f"The automated review hit an error at {_stamp()} before it could post its results, "
        "so **this MR has not been reviewed**. An administrator can see the cause on the "
        "review dashboard. Push a new commit, or re-trigger the review, to try again."
    )


def interrupted_note_body() -> str:
    return (
        f"{INTERRUPTED_NOTE_MARKER}\n\n"
        "An earlier review run on this MR stopped before it finished (the service was "
        f"restarted or crashed); noticed at {_stamp()}. That run posted no summary. "
        "A newer review, if one is running, will post its own results."
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


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


def _code_fence(code: str) -> str:
    """Backtick fence long enough that `code` can't close it early — GitLab
    (like CommonMark) accepts 4+ backticks when the body itself contains ```."""
    longest = max((len(m) for m in re.findall(r"`{3,}", code)), default=0)
    return "`" * max(3, longest + 1)


def _build_inline_body(comment: ReviewComment, applicable: bool = True) -> str:
    """
    Markdown for one inline diff comment. When the finding carries a CodeFix
    it's rendered as a GitLab suggestion block — the MR author gets an
    "Apply suggestion" button that commits the replacement directly:

        ```suggestion:-1+0
        <replacement for the commented line and the 1 line above it>
        ```

    The -N+M offsets are relative to the commented line (comment.line), which
    BaseAgent._parse_fix() guarantees lies inside the fix range. With
    `applicable` False (POST_FIX_SUGGESTIONS=false) the same code is shown
    read-only. Without a fix, the prose `suggestion` is shown as before.
    """
    body = (
        f"{_EMOJI[comment.severity]} **[{comment.severity.upper()}] {comment.title}**\n"
        f"*Agent: {comment.agent}*\n\n"
        f"{comment.description}"
    )
    fix = comment.fix
    if fix is not None and comment.line is not None and fix.covers(comment.line):
        fence = _code_fence(fix.replacement)
        span  = f"line {fix.start_line}" if fix.line_count == 1 else f"lines {fix.start_line}–{fix.end_line}"
        if applicable:
            above = comment.line - fix.start_line
            below = fix.end_line - comment.line
            body += (
                f"\n\n**Suggested fix** (replaces {span} — click *Apply suggestion* to commit it):\n"
                f"{fence}suggestion:-{above}+{below}\n{fix.replacement}\n{fence}"
            )
        else:
            body += f"\n\n**Suggested fix** (replaces {span}):\n{fence}\n{fix.replacement}\n{fence}"
    elif comment.suggestion:
        body += f"\n\n**Suggestion:**\n```\n{comment.suggestion}\n```"
    return body


def _build_summary(result: ReviewResult, failed_inline: list[ReviewComment]) -> str:
    header = (
        f"## 🤖 Automated Code Review\n\n"
        f"{result.summary}\n\n"
        f"---\n"
        f"**Issues:** "
        f"🔴 {result.critical_count} critical · "
        f"🟠 {result.high_count} high · "
        f"🟡 {result.medium_count} medium · "
        f"🔵 {result.low_count} low"
    )
    if result.fix_count:
        header += f" · 🛠️ {result.fix_count} with a ready-to-apply fix"
    if result.degraded:
        # Counts are a floor, not a tally — some agents never reported. The
        # reason is already spelled out in result.summary's banner.
        header += " · ⚠️ incomplete review"
    header += "\n"

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
