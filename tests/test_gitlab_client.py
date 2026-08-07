"""
tests/test_gitlab_client.py — Unit tests for the GitLab summary comment builder
and the API-based changed-file fetch (the fallback used when a full git clone
isn't available for static analysis).

Regression guard for a real bug: embedding a `Severity` enum directly in an
f-string (`f"{c.severity}"`) rendered as the literal "Severity.critical"
instead of "critical" in the posted MR comment.
"""

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


# ── progress note (post + delete) ─────────────────────────────────────────────

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
async def test_delete_mr_note_calls_expected_endpoint():
    client = _bare_client()

    response = MagicMock()
    response.raise_for_status = MagicMock()

    http = AsyncMock()
    http.delete = AsyncMock(return_value=response)

    with patch("app.gitlab.client.httpx.AsyncClient", return_value=_AsyncClientCtx(http)):
        await client.delete_mr_note("group/repo", 7, 42)

    http.delete.assert_awaited_once()
    called_url = http.delete.await_args.args[0]
    assert called_url == "https://gitlab.example.com/api/v4/projects/group%2Frepo/merge_requests/7/notes/42"


@pytest.mark.asyncio
async def test_post_progress_note_body_starts_with_marker():
    """delete_stale_progress_notes() matches on this exact prefix — if the
    posted body ever drifts from PROGRESS_NOTE_MARKER, cleanup silently stops
    finding its own notes."""
    from app.gitlab.client import PROGRESS_NOTE_MARKER

    client = _bare_client()
    client.post_mr_note = AsyncMock(return_value=99)

    note_id = await client.post_progress_note(1, 7)

    assert note_id == 99
    posted_body = client.post_mr_note.await_args.args[2]
    assert posted_body.startswith(PROGRESS_NOTE_MARKER)


@pytest.mark.asyncio
async def test_delete_stale_progress_notes_removes_only_marked_notes():
    from app.gitlab.client import PROGRESS_NOTE_MARKER

    client = _bare_client()
    client.list_mr_notes = AsyncMock(return_value=[
        {"id": 1, "body": PROGRESS_NOTE_MARKER + "\n\nsome text"},
        {"id": 2, "body": "a real human comment, unrelated"},
        {"id": 3, "body": PROGRESS_NOTE_MARKER + "\n\nanother leftover"},
    ])
    client.delete_mr_note = AsyncMock()

    deleted = await client.delete_stale_progress_notes(1, 7)

    assert deleted == 2
    deleted_ids = {call.args[2] for call in client.delete_mr_note.await_args_list}
    assert deleted_ids == {1, 3}


@pytest.mark.asyncio
async def test_delete_stale_progress_notes_returns_zero_when_none_stale():
    client = _bare_client()
    client.list_mr_notes = AsyncMock(return_value=[{"id": 1, "body": "unrelated comment"}])
    client.delete_mr_note = AsyncMock()

    deleted = await client.delete_stale_progress_notes(1, 7)

    assert deleted == 0
    client.delete_mr_note.assert_not_called()


@pytest.mark.asyncio
async def test_delete_stale_progress_notes_swallows_list_failure():
    """A transient GitLab API error while checking for stale notes must not
    block the new review from starting — it's a best-effort cleanup."""
    client = _bare_client()
    client.list_mr_notes = AsyncMock(side_effect=Exception("network blip"))

    deleted = await client.delete_stale_progress_notes(1, 7)

    assert deleted == 0
