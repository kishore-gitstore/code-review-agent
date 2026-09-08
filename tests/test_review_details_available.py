"""
tests/test_review_details_available.py — a finished review's details must
actually be reachable from the dashboard.

Two independent regressions lived here, both of which ended with the dashboard
showing nothing for a review that had completed normally:

1. The report JSON is the dashboard's ONLY source for a finished review, and
   it was written after print_console()/log_summary(). Those push the summary's
   emoji verdict (✅/⚠️/🔴 — the summary prompt asks for it) at stdout, which
   raises UnicodeEncodeError whenever stdout isn't UTF-8: the NSSM service's
   redirected log file, or any `> out.log`. save_json() was never reached, so
   the review left no trace.

2. GET /review/status/{instance}/{project_id}/{mr_iid} could not match a
   path-style GitLab project id ("group/repo"). The dashboard percent-encodes
   the slash, ASGI hands Starlette the *decoded* path, and the extra segment
   missed the route — a 404 the live drawer reported as "This review is no
   longer active".
"""

import io
import json
import sys
import pytest
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import reporting, server
from app.reporting import ExecutionReport
from app.utils.console import force_utf8_streams

# The verdicts that actually occur: the LLM is prompted for these three, and
# the no-findings and degraded summaries are hardcoded with the first two.
EMOJI_VERDICTS = [
    "🔴 **Request Changes** — critical SQL injection in the checkout path.",
    "⚠️ **Needs Discussion** — the retry loop needs another look.",
    "✅ **No significant issues found.** This MR looks good to merge.",
]


def _report(tmp_path, summary="✅ **No significant issues found.**") -> ExecutionReport:
    from datetime import datetime, timedelta
    from app.utils.models import ReviewResult

    r = ExecutionReport(
        repository="payments-api", branch="feat/checkout", instance="primary",
        project_id="group/payments-api", project_name="payments-api",
        mr_iid=418, mr_title="Add checkout", mr_author="alice",
    )
    t0 = datetime.now()
    r.started_at = t0
    r.on_llm_request("security", "app/checkout.py", t0, t0 + timedelta(seconds=1), True)
    result = ReviewResult(mr_iid=418, project_id="group/payments-api", summary=summary)
    result.compute_counts()
    r.result = result
    r.status = "success"
    r.finished_at = t0 + timedelta(seconds=2)
    return r


# ── 1. the report survives a stdout that can't render it ────────────────────

@contextmanager
def cp1252_stdout():
    """
    stdout as the NSSM service supplies it: a byte sink on the Windows ANSI
    code page, with no console and no say in the matter.

    Deliberately a context manager entered inside the test body, NOT a
    fixture: pytest resumes its global capture at the start of the call phase,
    which reassigns sys.stdout and silently discards whatever a fixture
    installed during setup. As a fixture this reached nothing and the tests
    below passed with the fix removed.
    """
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    try:
        yield
    finally:
        sys.stdout, sys.stderr = real_out, real_err


@pytest.mark.parametrize("summary", EMOJI_VERDICTS)
def test_report_is_saved_even_when_the_console_cannot_encode_it(tmp_path, summary):
    """The original failure, once per verdict the app actually emits: stdout
    can't encode the summary, log_summary raises mid-write, and the report has
    to reach disk anyway.

    The failure is injected rather than provoked with a real cp1252 stream:
    structlog binds sys.stdout on first use, which under pytest has already
    happened, so swapping sys.stdout here would not reach the logger and the
    test would pass with the bug present (it did).
    """
    report = _report(tmp_path, summary)
    cannot_encode = UnicodeEncodeError(
        "charmap", summary, 0, 1, "character maps to <undefined>")
    with patch.object(reporting, "REPORTS_DIR", tmp_path), \
         patch.object(ExecutionReport, "log_summary", side_effect=cannot_encode):
        path = report.persist_and_render()

    assert path is not None, "no report written — the dashboard would show nothing"
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["summary"] == summary


def test_report_is_saved_even_when_rendering_raises(tmp_path):
    """Persisting must not depend on rendering for any reason, not just
    encoding — a closed stream, a broken pipe, a bug in the table code."""
    report = _report(tmp_path)
    with patch.object(reporting, "REPORTS_DIR", tmp_path), \
         patch.object(ExecutionReport, "print_console", side_effect=RuntimeError("boom")):
        path = report.persist_and_render()

    assert path is not None and path.is_file()


def test_render_still_runs_when_it_can(tmp_path, capsys):
    report = _report(tmp_path)
    with patch.object(reporting, "REPORTS_DIR", tmp_path):
        path = report.persist_and_render()

    assert path is not None
    assert "LLM Request Statistics" in capsys.readouterr().out


def test_force_utf8_streams_switches_a_cp1252_stream_to_utf8():
    """The root-cause fix, rather than the belt-and-braces one: after this the
    console can actually show the verdict instead of merely not losing it.

    Asserts the stream's encoding rather than that print() stops raising: what
    print() reaches under pytest's capture is not this stream.
    """
    with cp1252_stdout():
        assert sys.stdout.encoding == "cp1252"  # what the service gets
        force_utf8_streams()

        encodings = (sys.stdout.encoding, sys.stderr.encoding)
        sys.stdout.write("🔴 **Request Changes**")  # would raise on cp1252
        sys.stdout.flush()

    assert encodings == ("utf-8", "utf-8")


def test_force_utf8_streams_is_safe_on_a_stream_it_cannot_reconfigure():
    real = sys.stdout
    sys.stdout = object()  # no .reconfigure at all
    try:
        force_utf8_streams()  # must not raise
    finally:
        sys.stdout = real


# ── 2. the live-status route reaches path-style project ids ─────────────────

@pytest.fixture
def client():
    with patch.dict(server._reviews, {}, clear=True):
        yield TestClient(server.create_app())


def _register(instance, project_id, mr_iid):
    server._reviews[server._rkey(instance, project_id, mr_iid)] = server.ReviewProgress(
        instance=instance, project_id=project_id, mr_iid=mr_iid,
        mr_title="Add checkout", status="running", started_at="2026-09-08T19:00:00",
        agents_all=["code_quality", "security"],
    )


@pytest.mark.parametrize("project_id, url_project", [
    ("group/payments-api", "group%2Fpayments-api"),   # the normal GitLab case
    ("group/sub/payments-api", "group%2Fsub%2Fpayments-api"),  # nested subgroups
    ("1234", "1234"),                                 # numeric id still works
])
def test_live_status_is_reachable_for_any_project_id_shape(client, project_id, url_project):
    _register("primary", project_id, 418)
    res = client.get(f"/review/status/primary/{url_project}/418")

    assert res.status_code == 200, (
        f"{res.status_code} for project_id {project_id!r} — the dashboard's live "
        f"drawer renders this as 'This review is no longer active.'"
    )
    body = res.json()
    assert body["project_id"] == project_id
    assert body["mr_iid"] == 418
    assert body["status"] == "running"


def test_live_status_still_404s_for_an_unknown_review(client):
    res = client.get("/review/status/primary/group%2Fpayments-api/999")

    assert res.status_code == 404
    # Route matched and the handler said so — not a routing miss ("Not Found").
    assert res.json()["detail"] == "No review found for this MR"


def test_live_status_reports_degraded_so_it_is_not_badged_a_plain_success(client):
    _register("primary", "group/payments-api", 418)
    server._finish("primary", "group/payments-api", 418, total=0, critical=0, high=0,
                   degraded=True)
    body = client.get("/review/status/primary/group%2Fpayments-api/418").json()

    assert body["status"] == "done"
    assert body["degraded"] is True
