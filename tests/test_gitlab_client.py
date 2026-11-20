"""
tests/test_gitlab_client.py — Unit tests for the GitLab summary comment builder
and the API-based changed-file fetch (the fallback used when a full git clone
isn't available for static analysis).

Regression guard for a real bug: embedding a `Severity` enum directly in an
f-string (`f"{c.severity}"`) rendered as the literal "Severity.critical"
instead of "critical" in the posted MR comment.
"""

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.gitlab.client import GitLabClient, _build_summary
from app.utils.models import ReviewComment, ReviewResult, Severity


class _AsyncClientCtx:
    """Minimal async-context-manager stand-in for `httpx.AsyncClient(...)`."""
    def __init__(self, client):
        self._client = client

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, *exc):
        return False


def _bare_client() -> GitLabClient:
    """A GitLabClient with just the attributes fetch_changed_files needs,
    bypassing __init__ so this test doesn't depend on real .env settings."""
    client = GitLabClient.__new__(GitLabClient)
    client.base_url = "https://gitlab.example.com/api/v4"
    client.headers = {"PRIVATE-TOKEN": "x"}
    return client


def _result(**overrides) -> ReviewResult:
    defaults = dict(
        mr_iid=1, project_id="group/repo", summary="Looks fine.",
        comments=[], static_analysis_notes=[],
    )
    defaults.update(overrides)
    result = ReviewResult(**defaults)
    result.compute_counts()
    return result


def test_summary_renders_severity_value_not_enum_repr():
    result = _result(comments=[
        ReviewComment(agent="security", severity=Severity.critical,
                      file_path="app/auth.py", line=3, title="SQL Injection", description="x"),
    ])
    body = _build_summary(result, failed_inline=[])

    assert "`critical`" in body
    assert "Severity.critical" not in body


def test_summary_includes_static_analysis_notes_even_with_no_comments():
    result = _result(static_analysis_notes=["ruff (python) skipped — ruff not found on PATH"])
    body = _build_summary(result, failed_inline=[])

    assert "Static Analysis" in body
    assert "ruff not found on PATH" in body


def test_summary_omits_static_analysis_section_when_no_notes():
    result = _result(comments=[
        ReviewComment(agent="security", severity=Severity.high,
                      file_path="app/auth.py", line=1, title="x", description="y"),
    ])
    body = _build_summary(result, failed_inline=[])
    assert "Static Analysis" not in body


def test_summary_footer_reflects_configured_provider_not_hardcoded_copilot():
    """Regression guard: the footer used to hardcode
    'Powered by GitHub Copilot (Claude Sonnet)' regardless of LLM_PROVIDER —
    e.g. a user running Ollama+qwen would see a Copilot/Claude credit."""
    result = _result(comments=[
        ReviewComment(agent="security", severity=Severity.high,
                      file_path="app/auth.py", line=1, title="x", description="y"),
    ])
    with patch("app.gitlab.client.get_active_model_label", return_value="ollama (qwen3-coder:latest)"):
        body = _build_summary(result, failed_inline=[])

    assert "Powered by ollama (qwen3-coder:latest)" in body
    assert "GitHub Copilot" not in body
    assert "Claude Sonnet" not in body


# ── fetch_changed_files (git-clone-free static analysis fallback) ────────────

@pytest.mark.asyncio
async def test_fetch_changed_files_writes_content(tmp_path):
    client = _bare_client()

    response = MagicMock()
    response.status_code = 200
    response.content = b"print('hi')\n"
    response.raise_for_status = MagicMock()

    http = AsyncMock()
    http.get = AsyncMock(return_value=response)

    with patch("app.gitlab.client.httpx.AsyncClient", return_value=_AsyncClientCtx(http)):
        ok, reason = await client.fetch_changed_files(1, ["app/main.py"], "abc123", str(tmp_path))

    assert ok is True
    assert reason == ""
    assert (tmp_path / "app" / "main.py").read_text() == "print('hi')\n"


@pytest.mark.asyncio
async def test_fetch_changed_files_skips_deleted_files(tmp_path):
    client = _bare_client()

    response = MagicMock()
    response.status_code = 404

    http = AsyncMock()
    http.get = AsyncMock(return_value=response)

    with patch("app.gitlab.client.httpx.AsyncClient", return_value=_AsyncClientCtx(http)):
        ok, reason = await client.fetch_changed_files(1, ["deleted.py"], "abc123", str(tmp_path))

    # Nothing fetched (the only file was deleted in this MR) — reported as failure
    # with a clear reason rather than silently pretending analysis ran.
    assert ok is False
    assert "could not fetch" in reason


@pytest.mark.asyncio
async def test_fetch_changed_files_reports_specific_reason_on_failure(tmp_path):
    client = _bare_client()

    http = AsyncMock()
    http.get = AsyncMock(side_effect=Exception("connection reset"))

    with patch("app.gitlab.client.httpx.AsyncClient", return_value=_AsyncClientCtx(http)):
        ok, reason = await client.fetch_changed_files(1, ["app/main.py"], "abc123", str(tmp_path))

    assert ok is False
    assert "app/main.py" in reason
    assert "connection reset" in reason


# ── progress note (post + edit) ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_post_mr_note_returns_note_id():
    client = _bare_client()

    response = MagicMock()
    response.json = MagicMock(return_value={"id": 42})
    response.raise_for_status = MagicMock()

    http = AsyncMock()
    http.post = AsyncMock(return_value=response)

    with patch("app.gitlab.client.httpx.AsyncClient", return_value=_AsyncClientCtx(http)):
        note_id = await client.post_mr_note(1, 7, "in progress")

    assert note_id == 42


@pytest.mark.asyncio
async def test_update_mr_note_puts_to_the_note_url():
    client = _bare_client()

    response = MagicMock()
    response.raise_for_status = MagicMock()

    http = AsyncMock()
    http.put = AsyncMock(return_value=response)

    with patch("app.gitlab.client.httpx.AsyncClient", return_value=_AsyncClientCtx(http)):
        await client.update_mr_note("group/repo", 7, 42, "summary")

    http.put.assert_awaited_once()
    assert http.put.await_args.args[0] == \
        "https://gitlab.example.com/api/v4/projects/group%2Frepo/merge_requests/7/notes/42"
    assert http.put.await_args.kwargs["json"] == {"body": "summary"}


@pytest.mark.asyncio
async def test_post_progress_note_body_starts_with_marker():
    """retire_progress_notes() matches on this exact prefix — if the posted
    body ever drifts from PROGRESS_NOTE_MARKER, a crashed run's placeholder
    would never be found again."""
    from app.gitlab.client import PROGRESS_NOTE_MARKER

    client = _bare_client()
    client.post_mr_note = AsyncMock(return_value=99)

    note_id = await client.post_progress_note(1, 7)

    assert note_id == 99
    posted_body = client.post_mr_note.await_args.args[2]
    assert posted_body.startswith(PROGRESS_NOTE_MARKER)


@pytest.mark.asyncio
async def test_retire_progress_notes_edits_only_marked_notes():
    from app.gitlab.client import INTERRUPTED_NOTE_MARKER, PROGRESS_NOTE_MARKER

    client = _bare_client()
    client.list_mr_notes = AsyncMock(return_value=[
        {"id": 1, "body": PROGRESS_NOTE_MARKER + "\n\nsome text"},
        {"id": 2, "body": "a real human comment, unrelated"},
        {"id": 3, "body": PROGRESS_NOTE_MARKER + "\n\nanother leftover"},
    ])
    client.update_mr_note = AsyncMock()

    retired = await client.retire_progress_notes(1, 7)

    assert retired == 2
    calls = client.update_mr_note.await_args_list
    assert {c.args[2] for c in calls} == {1, 3}
    assert all(c.args[3].startswith(INTERRUPTED_NOTE_MARKER) for c in calls)


@pytest.mark.asyncio
async def test_retire_progress_notes_returns_zero_when_none_stale():
    client = _bare_client()
    client.list_mr_notes = AsyncMock(return_value=[{"id": 1, "body": "unrelated comment"}])
    client.update_mr_note = AsyncMock()

    retired = await client.retire_progress_notes(1, 7)

    assert retired == 0
    client.update_mr_note.assert_not_called()


@pytest.mark.asyncio
async def test_retire_progress_notes_swallows_list_failure():
    """A transient GitLab API error while checking for stale notes must not
    block the new review from starting — it's a best-effort cleanup."""
    client = _bare_client()
    client.list_mr_notes = AsyncMock(side_effect=Exception("network blip"))

    retired = await client.retire_progress_notes(1, 7)

    assert retired == 0


# ── the review agent never deletes anything ───────────────────────────────────

def test_the_client_has_no_way_to_delete_a_note():
    assert not hasattr(GitLabClient, "delete_mr_note")
    assert not hasattr(GitLabClient, "delete_stale_progress_notes")


def test_every_gitlab_http_client_carries_the_guard():
    """The guard only protects requests sent through _http(). A new method
    that builds its own httpx.AsyncClient would silently bypass it."""
    import inspect
    import app.gitlab.client as mod
    src = inspect.getsource(mod)
    # The one construction is inside _http itself.
    assert src.count("httpx.AsyncClient(") == 1


_BASE = "https://gitlab.example.com/api/v4/projects/group%2Frepo"


@pytest.mark.asyncio
@pytest.mark.parametrize("method,url", [
    ("DELETE", f"{_BASE}/merge_requests/7"),                 # delete the MR
    ("DELETE", f"{_BASE}/merge_requests/7/notes/42"),        # even our own note
    ("DELETE", f"{_BASE}"),                                  # delete the project
    ("PUT",    f"{_BASE}/merge_requests/7"),                 # close/edit the MR
    ("PUT",    f"{_BASE}/merge_requests/7/merge"),           # merge it
    ("POST",   f"{_BASE}/merge_requests/7/approve"),
    ("POST",   f"{_BASE}/merge_requests"),                   # open a new MR
    ("PATCH",  f"{_BASE}/merge_requests/7/notes/42"),
    ("PUT",    f"{_BASE}/merge_requests/7/notes/abc"),
])
async def test_the_guard_refuses_anything_but_note_writes(method, url):
    from app.gitlab.client import ForbiddenGitLabRequest, _guard_request
    with pytest.raises(ForbiddenGitLabRequest):
        await _guard_request(httpx.Request(method, url))


@pytest.mark.asyncio
@pytest.mark.parametrize("method,url", [
    ("GET",  f"{_BASE}/merge_requests/7/changes"),
    ("POST", f"{_BASE}/merge_requests/7/notes"),
    ("POST", f"{_BASE}/merge_requests/7/discussions"),
    ("PUT",  f"{_BASE}/merge_requests/7/notes/42"),
])
async def test_the_guard_allows_what_a_review_needs(method, url):
    from app.gitlab.client import _guard_request
    await _guard_request(httpx.Request(method, url))


@pytest.mark.asyncio
async def test_the_guard_runs_on_the_wire():
    """End to end through a real httpx client: a forbidden request never
    reaches the transport."""
    from app.gitlab.client import ForbiddenGitLabRequest, _http

    sent = []
    transport = httpx.MockTransport(lambda req: sent.append(req) or httpx.Response(204))
    async with _http(5) as c:
        c._transport = transport
        with pytest.raises(ForbiddenGitLabRequest):
            await c.delete(f"{_BASE}/merge_requests/7")
        await c.get(f"{_BASE}/merge_requests/7")
    assert [r.method for r in sent] == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mr,note", [(7, None), (7, 0), (7, "42/../.."), (None, 5), (7, True), (0, 5), ("", 5)])
async def test_update_mr_note_refuses_ids_that_could_reach_the_mr(mr, note):
    with pytest.raises(ValueError):
        await _bare_client().update_mr_note("g/r", mr, note, "x")


# ── the progress note becomes the summary ─────────────────────────────────────

@pytest.mark.asyncio
async def test_post_review_edits_the_placeholder_into_the_summary(monkeypatch):
    from app.settings import get_settings
    monkeypatch.setattr(get_settings(), "post_inline_comments", False)
    monkeypatch.setattr(get_settings(), "post_summary_comment", True)
    client = _bare_client()
    client.update_mr_note = AsyncMock()
    client.post_mr_note = AsyncMock()

    await client.post_review(_result(), {"diff_refs": {}}, progress_note_id=555)

    client.update_mr_note.assert_awaited_once()
    assert client.update_mr_note.await_args.args[2] == 555
    client.post_mr_note.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_review_posts_the_summary_if_the_placeholder_cannot_be_edited(monkeypatch):
    from app.settings import get_settings
    monkeypatch.setattr(get_settings(), "post_inline_comments", False)
    monkeypatch.setattr(get_settings(), "post_summary_comment", True)
    client = _bare_client()
    client.update_mr_note = AsyncMock(side_effect=Exception("404 note removed by hand"))
    client.post_mr_note = AsyncMock()

    await client.post_review(_result(), {"diff_refs": {}}, progress_note_id=555)

    client.post_mr_note.assert_awaited_once()
