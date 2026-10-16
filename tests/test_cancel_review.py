"""
tests/test_cancel_review.py — a review in progress can be stopped, and the
merge request is told.

The dangerous failure here is a *silent* cancellation. A review that stops
without saying so leaves the MR showing either nothing at all or a stale
"review in progress" note, and the natural reading of a quiet bot is "it
looked and found nothing" — an absence of evidence read as evidence of
absence, on exactly the merge request nobody checked.

So the guarantees under test are:

  * cancelling actually interrupts the pipeline (it isn't a flag the LLM call
    gets to ignore for another four minutes);
  * the placeholder note is removed and replaced with a permanent record
    naming who cancelled it;
  * the saved report says "cancelled", not "success with zero findings";
  * and cancelling something that isn't running is refused rather than
    silently reported as done.
"""

import asyncio
import json
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app import reporting, server
from app.settings import get_settings

INSTANCE, PROJECT, MR = "primary", "group/payments-api", 418
KEY = server._rkey(INSTANCE, PROJECT, MR)


def _fake_gitlab() -> MagicMock:
    """A GitLab client that answers everything _run_review asks of it."""
    g = MagicMock()
    g.delete_stale_progress_notes = AsyncMock(return_value=0)
    g.post_progress_note = AsyncMock(return_value=555)
    g.delete_mr_note = AsyncMock(return_value=None)
    g.post_cancelled_note = AsyncMock(return_value=556)
    g.post_review = AsyncMock(return_value=None)
    g.get_mr_details = AsyncMock(return_value={
        "title": "Add checkout", "description": "", "source_branch": "feat/checkout",
        "target_branch": "main", "author": {"username": "alice", "name": "Alice A"},
        "web_url": "https://gitlab.example.com/group/payments-api/-/merge_requests/418",
        "diff_refs": {"head_sha": "abc123"},
    })
    g.get_mr_diff = AsyncMock(return_value=(
        "diff --git a/app/checkout.py b/app/checkout.py\n"
        "--- a/app/checkout.py\n+++ b/app/checkout.py\n"
        "@@ -1,2 +1,3 @@\n def pay(x):\n+    total = x * 1.2\n"
    ))
    g.get_project = AsyncMock(return_value={"path_with_namespace": "group/payments-api"})
    return g


def _stub_coordinator(on_run):
    """CoordinatorAgent stand-in whose run() is whatever the test supplies."""
    coordinator = SimpleNamespace(
        agents=[SimpleNamespace(name="code_quality"), SimpleNamespace(name="security")],
        run=on_run,
    )
    return MagicMock(return_value=coordinator)


@pytest.fixture(autouse=True)
def _clean_trackers(tmp_path, monkeypatch):
    server._reviews.clear()
    server._tasks.clear()
    server._detached.clear()
    # Reports land in a temp dir, and the clone step is skipped — neither is
    # what these tests are about, and both would reach the real filesystem.
    monkeypatch.setattr(reporting, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(get_settings(), "clone_for_static_analysis", False)
    yield
    server._reviews.clear()
    server._tasks.clear()
    server._detached.clear()


async def _drain_detached():
    """Let the detached MR-update task finish. It is deliberately not awaited
    by the review (see _announce_cancellation), so a test has to."""
    for _ in range(10):
        pending = [t for t in list(server._detached) if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)


# ── the real thing: interrupt a running pipeline ────────────────────────────

@pytest.mark.asyncio
async def test_cancelling_stops_the_pipeline_and_records_it_on_the_merge_request(tmp_path):
    gitlab = _fake_gitlab()
    reached_the_agents = asyncio.Event()

    async def never_finishes(ctx, **callbacks):
        reached_the_agents.set()
        await asyncio.sleep(3600)   # stands in for a long LLM call
        raise AssertionError("the coordinator was allowed to finish")

    with patch("app.server.get_gitlab_client", return_value=gitlab), \
         patch("app.server.CoordinatorAgent", _stub_coordinator(never_finishes)):
        task = asyncio.create_task(
            server._run_review_bg(INSTANCE, PROJECT, MR, "Add checkout")
        )
        await asyncio.wait_for(reached_the_agents.wait(), timeout=5)

        # What POST /review/{...}/cancel does.
        server._reviews[KEY].cancel_requested = True
        server._reviews[KEY].cancelled_by = "Alice Admin"
        server._tasks[KEY].cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        await _drain_detached()

    progress = server._reviews[KEY]
    assert progress.status == "cancelled"
    assert progress.finished_at is not None
    assert progress.agents_running == []

    # The placeholder is gone and a permanent record took its place.
    assert gitlab.delete_stale_progress_notes.await_count >= 1
    gitlab.post_cancelled_note.assert_awaited_once()
    assert gitlab.post_cancelled_note.await_args.kwargs["cancelled_by"] == "Alice Admin"

    # And nothing was posted as if it were a finished review.
    gitlab.post_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_cancelled_review_is_saved_as_cancelled_not_as_a_clean_success(tmp_path):
    """The report is the dashboard's only record of a finished review. A
    cancelled one saved as "success, 0 issues" would read as an all-clear."""
    gitlab = _fake_gitlab()
    running = asyncio.Event()

    async def never_finishes(ctx, **callbacks):
        running.set()
        await asyncio.sleep(3600)

    with patch("app.server.get_gitlab_client", return_value=gitlab), \
         patch("app.server.CoordinatorAgent", _stub_coordinator(never_finishes)):
        task = asyncio.create_task(server._run_review_bg(INSTANCE, PROJECT, MR, "Add checkout"))
        await asyncio.wait_for(running.wait(), timeout=5)
        server._reviews[KEY].cancelled_by = "Alice Admin"
        server._tasks[KEY].cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _drain_detached()

    saved = list(tmp_path.glob("review_*.json"))
    assert len(saved) == 1, "a cancelled review left no report behind"
    report = json.loads(saved[0].read_text(encoding="utf-8"))

    assert report["status"] == "cancelled"
    assert "Alice Admin" in report["error"]
    assert report["total_issues"] == 0
    # Filename stamped from a real start time, not "unknown".
    assert "unknown" not in saved[0].name


@pytest.mark.asyncio
async def test_cancelling_before_the_review_starts_still_records_it(tmp_path):
    """A review can be cancelled while queued behind MAX_PARALLEL_REVIEWS,
    before it has fetched anything. It must still leave a trace — the
    in-memory tracker only keeps the last 50 entries."""
    gitlab = _fake_gitlab()

    # Hold the semaphore so the review can't get past the queue.
    blocker = asyncio.Semaphore(1)
    await blocker.acquire()

    with patch("app.server._review_semaphore", blocker), \
         patch("app.server.get_gitlab_client", return_value=gitlab), \
         patch("app.server.CoordinatorAgent", _stub_coordinator(AsyncMock())):
        task = asyncio.create_task(server._run_review_bg(INSTANCE, PROJECT, MR, "Add checkout"))
        while KEY not in server._tasks:
            await asyncio.sleep(0)
        await asyncio.sleep(0)  # let it reach the semaphore await

        server._reviews[KEY].cancelled_by = "Alice Admin"
        server._tasks[KEY].cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _drain_detached()

    assert server._reviews[KEY].status == "cancelled"
    gitlab.get_mr_details.assert_not_awaited()      # never got as far as GitLab
    gitlab.post_cancelled_note.assert_awaited_once()
    assert len(list(tmp_path.glob("review_*.json"))) == 1


@pytest.mark.asyncio
async def test_the_task_handle_is_released_so_a_rerun_is_possible(tmp_path):
    gitlab = _fake_gitlab()
    running = asyncio.Event()

    async def never_finishes(ctx, **callbacks):
        running.set()
        await asyncio.sleep(3600)

    with patch("app.server.get_gitlab_client", return_value=gitlab), \
         patch("app.server.CoordinatorAgent", _stub_coordinator(never_finishes)):
        task = asyncio.create_task(server._run_review_bg(INSTANCE, PROJECT, MR, ""))
        await asyncio.wait_for(running.wait(), timeout=5)
        server._tasks[KEY].cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _drain_detached()

    assert KEY not in server._tasks, "a cancelled review left its task handle behind"


# ── the endpoint ────────────────────────────────────────────────────────────

def _register(status="running", **overrides):
    p = server.ReviewProgress(
        instance=INSTANCE, project_id=PROJECT, mr_iid=MR, mr_title="Add checkout",
        status=status, started_at="2026-09-08T19:00:00",
        agents_all=["code_quality", "security"], agents_done=["code_quality"],
        **overrides,
    )
    server._reviews[KEY] = p
    return p


CANCEL_URL = f"/review/{INSTANCE}/group%2Fpayments-api/{MR}/cancel"


def test_cancelling_an_unknown_review_is_a_404(signed_in_client):
    assert signed_in_client.post(CANCEL_URL).status_code == 404


@pytest.mark.parametrize("status", ["done", "error", "cancelled"])
def test_cancelling_a_finished_review_is_refused(signed_in_client, status):
    """409 rather than a cheerful 200: reporting "cancelled" for a review that
    already posted its findings would be a lie about what's on the MR."""
    _register(status=status)

    res = signed_in_client.post(CANCEL_URL)

    assert res.status_code == 409
    assert status in res.json()["detail"]


def test_cancelling_attributes_the_request_and_interrupts_the_task(signed_in_client):
    _register()
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(asyncio.sleep(3600))
        server._tasks[KEY] = task

        res = signed_in_client.post(CANCEL_URL)

        assert res.status_code == 202
        assert res.json()["status"] == "cancelling"
        # Attribution is recorded before the interrupt, because the task's own
        # handler reads it back to write the MR note.
        assert server._reviews[KEY].cancel_requested is True
        assert server._reviews[KEY].cancelled_by == "Test User"
        assert task.cancelled() or task.cancelling()
    finally:
        loop.close()


def test_a_record_with_no_live_task_is_closed_out_rather_than_left_running(signed_in_client):
    """The tracker and the task registry are both in-memory and normally move
    together. If they ever don't, a row stuck at "running" forever is worse
    than one closed out honestly."""
    _register()  # registered, but nothing in server._tasks

    with patch("app.server._announce_cancellation", AsyncMock()):
        res = signed_in_client.post(CANCEL_URL)

    assert res.status_code == 202
    assert res.json()["status"] == "cancelled"
    assert server._reviews[KEY].status == "cancelled"


def test_the_cancel_is_visible_on_the_live_status_endpoint(signed_in_client):
    """The dashboard polls this to show "cancelling…" — without it, a slow
    cancel looks like a button that did nothing."""
    p = _register()
    p.cancel_requested = True
    p.cancelled_by = "Alice Admin"

    body = signed_in_client.get(f"/review/status/{INSTANCE}/group%2Fpayments-api/{MR}").json()

    assert body["cancel_requested"] is True
    assert body["cancelled_by"] == "Alice Admin"


def test_a_cancelled_review_is_still_listed_while_it_ages_out(signed_in_client):
    _register()
    server._mark_cancelled(INSTANCE, PROJECT, MR)

    listed = signed_in_client.get("/review/active").json()

    assert [r["status"] for r in listed] == ["cancelled"]


def test_marking_cancelled_twice_does_not_move_the_finish_time(signed_in_client):
    """_mark_cancelled is called from both the pipeline's handler and its outer
    wrapper, so it has to be idempotent."""
    _register()
    server._mark_cancelled(INSTANCE, PROJECT, MR)
    first_finish = server._reviews[KEY].finished_at

    server._mark_cancelled(INSTANCE, PROJECT, MR)

    assert server._reviews[KEY].finished_at == first_finish


# ── the note itself ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_cancellation_note_says_the_mr_was_not_reviewed():
    """The whole point of the note. "No findings" must not be readable as
    "no problems"."""
    from app.gitlab.client import CANCELLED_NOTE_MARKER, GitLabClient

    client = GitLabClient.__new__(GitLabClient)
    client.post_mr_note = AsyncMock(return_value=1)

    await client.post_cancelled_note(
        "group/payments-api", 418, cancelled_by="Alice Admin",
        agents_done=1, agents_total=4, duration_s=95,
    )

    body = client.post_mr_note.await_args.args[2]
    assert body.startswith(CANCELLED_NOTE_MARKER)
    assert "no findings were posted" in body.lower()
    assert "not reviewed" in body.lower()
    assert "Alice Admin" in body
    assert "1 of 4" in body
    assert "1m 35s" in body


@pytest.mark.asyncio
async def test_the_cancellation_note_is_not_swept_away_by_the_next_run():
    """delete_stale_progress_notes() matches the progress marker by prefix. If
    the cancelled note shared it, the next review would quietly erase the
    audit trail it exists to leave."""
    from app.gitlab.client import CANCELLED_NOTE_MARKER, PROGRESS_NOTE_MARKER, GitLabClient

    client = GitLabClient.__new__(GitLabClient)
    client.list_mr_notes = AsyncMock(return_value=[
        {"id": 1, "body": f"{PROGRESS_NOTE_MARKER}\n\nplaceholder"},
        {"id": 2, "body": f"{CANCELLED_NOTE_MARKER}\n\nrecord"},
    ])
    client.delete_mr_note = AsyncMock(return_value=None)

    deleted = await client.delete_stale_progress_notes("group/payments-api", 418)

    assert deleted == 1
    assert client.delete_mr_note.await_args.args[2] == 1


# ── cancellation must not be retried ────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("module_name, class_name", [
    ("app.llm.ollama_client",  "OllamaClient"),
    ("app.llm.claude_client",  "ClaudeClient"),
    ("app.llm.openai_client",  "OpenAIClient"),
    ("app.llm.copilot_client", "CopilotClient"),
])
async def test_a_cancelled_llm_request_is_not_retried(module_name, class_name, monkeypatch):
    """
    generate() is wrapped in tenacity, and tenacity catches BaseException —
    so asyncio.CancelledError lands in its retry machinery like any other
    failure. Without an explicit exclusion, cancelling a review would fire
    two or three *fresh* LLM requests on the way out instead of stopping.
    """
    import importlib
    module = importlib.import_module(module_name)
    client = getattr(module, class_name).__new__(getattr(module, class_name))

    attempts = 0

    class _Cancelling:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self):
            nonlocal attempts
            attempts += 1
            raise asyncio.CancelledError()
        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(module.httpx, "AsyncClient", _Cancelling)
    for attr, value in (("model", "m"), ("base_url", "http://x"), ("api_key", "k"),
                        ("token", "k"), ("timeout", 5), ("_semaphore", asyncio.Semaphore(1))):
        if not hasattr(client, attr):
            try:
                setattr(client, attr, value)
            except AttributeError:
                pass

    with pytest.raises(asyncio.CancelledError):
        await client.generate("system", "user")

    assert attempts == 1, f"{class_name} retried a cancelled request {attempts} times"
