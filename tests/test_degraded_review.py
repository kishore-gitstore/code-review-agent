"""
tests/test_degraded_review.py — a failed model request must never be reported
as a clean review.

BaseAgent.review deliberately swallows its own LLM errors and returns no
findings, so one broken agent can't sink a whole review. The flip side is that
an empty findings list means either "nothing wrong" or "nothing ran", and
those used to be indistinguishable: with Ollama unreachable a review finished
as status=success, 0 issues, summary "✅ No significant issues found. This MR
looks good to merge." — and posted that to the MR as an approval.

Guarded here: ReviewResult.degraded/agent_failures (the count that makes the
difference visible), the coordinator's refusal to summarise a non-review as an
all-clear, and ExecutionReport's derived "partial" status.
"""

import json
import pytest
from unittest.mock import AsyncMock, patch

from app.agents.coordinator import CoordinatorAgent
from app.gitlab.client import _build_summary
from app.reporting import ExecutionReport
from app.utils.models import MRContext, ReviewComment, ReviewResult, Severity


def _ctx(body: str) -> MRContext:
    return MRContext(
        project_id=1,
        mr_iid=7,
        title="Add login endpoint",
        description="",
        author="alice",
        source_branch="feat/login",
        target_branch="main",
        diff=f"""diff --git a/app/auth.py b/app/auth.py
--- a/app/auth.py
+++ b/app/auth.py
@@ -1,2 +1,3 @@
 def login(email):
{body}
""",
        changed_files=["app/auth.py"],
    )


# Deliberately bland: SecurityAgent.review runs regex checks (eval, hardcoded
# secret, SQL built by interpolation) BEFORE it ever calls the LLM, and those
# fire whether or not the model responds. A diff that trips one can't be used
# to test "no findings at all", which is the case the false-green bug lived in.
CTX = _ctx("+    user = lookup(email)")

# Trips SecurityAgent's _SQL_CONCAT_RE without the LLM's involvement.
SQLI_CTX = _ctx('+    return db.execute(f"SELECT * FROM users WHERE email=\'{email}\'")')

FINDING = [{
    "file_path": "app/auth.py",
    "line": 2,
    "severity": "critical",
    "title": "SQL Injection",
    "description": "Unsanitised input interpolated into a query.",
    "suggestion": "Use a parameterised query.",
    "fix": None,
}]

GOOD_TO_MERGE = "looks good to merge"


def _clients(agent_generate, summary_generate=None):
    """Patch the agent-side and coordinator-side LLM clients independently, so
    a test can fail one without the other."""
    agent_client = AsyncMock()
    agent_client.generate = agent_generate
    coord_client = AsyncMock()
    coord_client.generate = summary_generate or AsyncMock(return_value="⚠️ Needs discussion.")
    return (
        patch("app.agents.base_agent.get_llm_client", return_value=agent_client),
        patch("app.agents.coordinator.get_llm_client", return_value=coord_client),
    )


# ── the failure path ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_total_llm_outage_is_not_reported_as_a_clean_review():
    """The original bug: every agent request fails, so there are no findings,
    and the review declares the MR mergeable."""
    agent_patch, coord_patch = _clients(
        AsyncMock(side_effect=RuntimeError("UnsupportedProtocol")))
    with agent_patch, coord_patch:
        coordinator = CoordinatorAgent()
        result = await coordinator.run(CTX)

    assert result.agent_requests == len(coordinator.agents)
    assert result.agent_failures == len(coordinator.agents)
    assert result.degraded is True
    assert result.total_issues == 0            # still no findings...
    assert GOOD_TO_MERGE not in result.summary  # ...but no longer an all-clear
    assert "incomplete" in result.summary.lower()
    assert "not an all-clear" in result.summary.lower()


@pytest.mark.asyncio
async def test_partial_outage_keeps_findings_and_still_flags_incompleteness():
    """One agent down: the findings the others produced are real and kept, but
    the summary must not present them as the complete picture."""
    calls = {"n": 0}

    async def flaky(_system, _user):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection refused")
        return json.dumps(FINDING)

    agent_patch, coord_patch = _clients(AsyncMock(side_effect=flaky))
    with agent_patch, coord_patch:
        coordinator = CoordinatorAgent()
        result = await coordinator.run(CTX)

    assert result.agent_failures == 1
    assert result.degraded is True
    assert result.total_issues >= 1
    assert "incomplete" in result.summary.lower()
    assert "1 of" in result.summary  # names how much of the review is missing


@pytest.mark.asyncio
async def test_summary_llm_failure_alone_marks_the_result_degraded():
    """Agents all reported, but the summary call failed and fell back to a
    count — the findings are complete, yet the verdict was never written."""
    agent_patch, coord_patch = _clients(
        AsyncMock(return_value=json.dumps(FINDING)),
        summary_generate=AsyncMock(side_effect=RuntimeError("boom")),
    )
    with agent_patch, coord_patch:
        coordinator = CoordinatorAgent()
        result = await coordinator.run(CTX)

    assert result.agent_failures == 0
    assert result.summary_failed is True
    assert result.degraded is True
    assert GOOD_TO_MERGE not in result.summary


@pytest.mark.asyncio
async def test_rule_based_findings_survive_an_outage_but_dont_imply_completeness():
    """SecurityAgent's regex checks don't need the LLM, so a total outage can
    still yield findings. Those are real and must be kept — but the review is
    no more complete for having them, and must not read as a finished verdict."""
    agent_patch, coord_patch = _clients(
        AsyncMock(side_effect=RuntimeError("connection refused")),
        summary_generate=AsyncMock(side_effect=RuntimeError("connection refused")),
    )
    with agent_patch, coord_patch:
        coordinator = CoordinatorAgent()
        result = await coordinator.run(SQLI_CTX)

    assert any(c.agent == "security" and c.title == "SQL injection risk"
               for c in result.comments)
    assert result.agent_failures == len(coordinator.agents)
    assert result.summary_failed is True
    assert result.degraded is True
    assert GOOD_TO_MERGE not in result.summary
    assert "incomplete" in result.summary.lower()


# ── the healthy path stays healthy ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_clean_review_is_still_an_all_clear():
    """Regression guard the other way: with every request succeeding and
    nothing found, the MR must still be reported as good to merge."""
    agent_patch, coord_patch = _clients(AsyncMock(return_value="[]"))
    with agent_patch, coord_patch:
        coordinator = CoordinatorAgent()
        result = await coordinator.run(CTX)

    assert result.agent_failures == 0
    assert result.summary_failed is False
    assert result.degraded is False
    assert GOOD_TO_MERGE in result.summary


@pytest.mark.asyncio
async def test_findings_review_is_not_flagged_incomplete():
    agent_patch, coord_patch = _clients(AsyncMock(return_value=json.dumps(FINDING)))
    with agent_patch, coord_patch:
        coordinator = CoordinatorAgent()
        result = await coordinator.run(CTX)

    assert result.degraded is False
    assert "incomplete" not in result.summary.lower()


# ── ExecutionReport's derived status ─────────────────────────────────────────

def _report(**kw) -> ExecutionReport:
    return ExecutionReport(repository="repo", branch="feat/login", **kw)


def _record(report, *statuses) -> None:
    from datetime import datetime
    now = datetime.now()
    for ok in statuses:
        report.on_llm_request("code_quality", "app/auth.py", now, now, ok)


def test_status_degrades_to_partial_when_an_llm_request_failed():
    """The caller only knows the review didn't raise, so it sets "success".
    The failed request has to override that on its own."""
    report = _report()
    _record(report, True, False, True)
    report.status = "success"

    assert report.status == "partial"
    assert report.llm_requests_failed == 1
    assert "1 of 3" in report.error
    assert report.summary_dict()["status"] == "partial"


def test_status_stays_success_when_every_request_succeeded():
    report = _report()
    _record(report, True, True)
    report.status = "success"

    assert report.status == "success"
    assert report.error is None


def test_partial_never_masks_an_outright_failure():
    """failed/cancelled are worse than partial and must survive the derivation
    — including their explicit error message."""
    for assigned in ("failed", "cancelled"):
        report = _report()
        _record(report, False)
        report.status = assigned
        report.error = "clone failed"

        assert report.status == assigned
        assert report.error == "clone failed"


def test_status_is_running_until_the_caller_sets_it():
    report = _report()
    _record(report, False)

    assert report.status == "running"


# ── what the MR author sees ─────────────────────────────────────────────────

def test_mr_note_marks_the_issue_counts_as_incomplete():
    degraded = ReviewResult(
        mr_iid=7, project_id=1, summary="⚠️ Review incomplete.",
        comments=[ReviewComment(agent="security", severity=Severity.high,
                                file_path="app/auth.py", line=2,
                                title="SQL Injection", description="d")],
        agent_requests=4, agent_failures=3,
    )
    degraded.compute_counts()
    with patch("app.gitlab.client.get_active_model_label", return_value="m"):
        body = _build_summary(degraded, [])

    assert "incomplete review" in body


def test_mr_note_for_a_complete_review_has_no_incomplete_marker():
    clean = ReviewResult(mr_iid=7, project_id=1, summary="✅ Approve.",
                         agent_requests=4, agent_failures=0)
    clean.compute_counts()
    with patch("app.gitlab.client.get_active_model_label", return_value="m"):
        body = _build_summary(clean, [])

    assert "incomplete review" not in body
