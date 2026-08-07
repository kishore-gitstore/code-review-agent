"""
tests/test_coordinator.py — Integration tests for the CoordinatorAgent
"""

import asyncio
import pytest
import json
from unittest.mock import AsyncMock, patch

from app.agents.coordinator import CoordinatorAgent
from app.utils.models       import MRContext, ReviewComment, Severity

SAMPLE_CTX = MRContext(
    project_id=1,
    mr_iid=10,
    title="Refactor database layer",
    description="",
    author="alice",
    source_branch="refactor/db",
    target_branch="main",
    diff="""diff --git a/db/queries.py b/db/queries.py
+def get_user(uid):
+    return db.execute(f"SELECT * FROM users WHERE id={uid}")
""",
    changed_files=["db/queries.py"],
)

FINDING = [{
    "file_path": "db/queries.py",
    "line": 2,
    "severity": "critical",
    "title": "SQL Injection",
    "description": "Unsanitised input in query.",
    "suggestion": None,
}]

SUMMARY = "🔴 **Request Changes** — Critical SQL injection found. Fix before merging."


@pytest.fixture
def mock_ollama():
    with patch("app.agents.base_agent.get_llm_client") as agent_mock, \
         patch("app.agents.coordinator.get_llm_client") as coord_mock:

        agent_client   = AsyncMock()
        agent_client.generate = AsyncMock(return_value=json.dumps(FINDING))
        agent_mock.return_value = agent_client

        coord_client   = AsyncMock()
        coord_client.generate = AsyncMock(return_value=SUMMARY)
        coord_mock.return_value = coord_client

        yield


@pytest.mark.asyncio
async def test_coordinator_runs_all_agents(mock_ollama):
    coordinator = CoordinatorAgent()
    result      = await coordinator.run(SAMPLE_CTX)

    assert result.mr_iid      == 10
    assert result.total_issues >= 1
    assert result.critical_count >= 1
    assert result.summary == SUMMARY


@pytest.mark.asyncio
async def test_coordinator_deduplication(mock_ollama):
    """When multiple agents return the same finding, it should be deduplicated."""
    coordinator = CoordinatorAgent()
    result      = await coordinator.run(SAMPLE_CTX)
    titles = [c.title for c in result.comments]
    # After dedup, "SQL Injection" should appear at most once per (file, line)
    assert titles.count("SQL Injection") <= 1


@pytest.mark.asyncio
async def test_coordinator_severity_filter(mock_ollama):
    """Low findings should still pass through with default config."""
    coordinator = CoordinatorAgent()
    result      = await coordinator.run(SAMPLE_CTX)
    for c in result.comments:
        assert c.severity in list(Severity)


@pytest.mark.asyncio
async def test_coordinator_surfaces_specific_reason_when_no_checkout(mock_ollama):
    """When static analysis can't run (no repo_root), the review result must
    carry the *specific* reason (e.g. why cloning failed) rather than a vague
    generic message the user can't act on."""
    reason = "static analysis skipped — git clone failed: fatal: repository not found"
    ctx = SAMPLE_CTX.model_copy(update={"repo_root_note": reason})

    coordinator = CoordinatorAgent()
    result      = await coordinator.run(ctx)

    assert result.static_analysis_notes == [reason]


def test_coordinator_defaults_to_sequential_dispatch():
    """Regression guard: AGENT_CONCURRENCY must default to 1. This project's
    primary use case is self-hosted Ollama, which typically can't parallelise
    generation anyway — a higher default silently reintroduces the
    self-inflicted-timeout bug for every user who doesn't know to set it."""
    assert CoordinatorAgent()._agent_concurrency == 1


@pytest.mark.asyncio
async def test_coordinator_findings_have_real_file_path(mock_ollama):
    """Rule-based quick checks must report the actual changed file, not
    'unknown', so the posted location is actionable."""
    coordinator = CoordinatorAgent()
    result      = await coordinator.run(SAMPLE_CTX)
    for c in result.comments:
        assert c.file_path != "unknown"


def test_scope_to_diff_drops_findings_outside_changed_lines():
    """Static analyzers scan the whole current file, not just the diff, so
    without this a review would surface pre-existing issues on untouched
    lines as if this MR introduced them."""
    comments = [
        ReviewComment(agent="static:ruff", severity=Severity.medium,
                      file_path="app.py", line=2, title="in-diff", description=""),
        ReviewComment(agent="static:ruff", severity=Severity.medium,
                      file_path="app.py", line=99, title="pre-existing", description=""),
        ReviewComment(agent="static:ruff", severity=Severity.low,
                      file_path="app.py", line=None, title="file-level", description=""),
        ReviewComment(agent="static:ruff", severity=Severity.low,
                      file_path="other.py", line=2, title="different-file", description=""),
    ]
    changed_line_map = {"app.py": {2, 5}}

    kept, dropped = CoordinatorAgent._scope_to_diff(comments, changed_line_map)

    assert [c.title for c in kept] == ["in-diff", "file-level"]
    assert dropped == 2


@pytest.mark.asyncio
async def test_coordinator_drops_static_findings_outside_the_diff(mock_ollama):
    """End-to-end: a static finding on a line the diff never touched must not
    appear in the final result, even though the file itself was changed."""
    # SAMPLE_CTX.diff has no real hunk header (@@ ... @@), so it can't be used
    # to test line-level scoping — changed_lines needs one to know where the
    # new-file line numbers actually start.
    diff_with_hunk_header = """diff --git a/db/queries.py b/db/queries.py
--- a/db/queries.py
+++ b/db/queries.py
@@ -1,2 +1,2 @@
 def get_user(uid):
-    return db.execute("SELECT * FROM users")
+    return db.execute(f"SELECT * FROM users WHERE id={uid}")
"""
    ctx = SAMPLE_CTX.model_copy(update={"diff": diff_with_hunk_header, "repo_root": "/fake/repo"})

    pre_existing = ReviewComment(
        agent="static:ruff", severity=Severity.medium,
        file_path="db/queries.py", line=99, title="Pre-existing lint issue", description="",
    )
    in_diff = ReviewComment(
        agent="static:ruff", severity=Severity.medium,
        file_path="db/queries.py", line=2, title="Actually in this diff", description="",
    )

    with patch("app.static_analysis.runner.StaticAnalysisRunner.run",
               new=AsyncMock(return_value=([pre_existing, in_diff], []))):
        coordinator = CoordinatorAgent()
        result = await coordinator.run(ctx)

    titles = [c.title for c in result.comments]
    assert "Pre-existing lint issue" not in titles
    assert "Actually in this diff" in titles


@pytest.mark.asyncio
async def test_coordinator_agent_concurrency_1_runs_strictly_in_series():
    """AGENT_CONCURRENCY=1 must serialize every review() call end-to-end —
    not just cap how many agents *start* at once. This is the fix for
    CPU-only local Ollama setups, where a single model instance can't truly
    parallelise and concurrent requests just queue behind each other until
    each one's own client-side timeout expires."""
    in_flight = {"current": 0, "max": 0}

    async def slow_generate(system_prompt, user_prompt):
        in_flight["current"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["current"])
        await asyncio.sleep(0.02)
        in_flight["current"] -= 1
        return json.dumps([])

    multi_file_diff = SAMPLE_CTX.diff + (
        "diff --git a/db/other.py b/db/other.py\n"
        "+x = 1\n"
    )
    ctx = SAMPLE_CTX.model_copy(update={
        "diff": multi_file_diff,
        "changed_files": ["db/queries.py", "db/other.py"],
    })

    with patch("app.agents.base_agent.get_llm_client") as agent_mock, \
         patch("app.agents.coordinator.get_llm_client") as coord_mock:
        agent_client = AsyncMock()
        agent_client.generate = AsyncMock(side_effect=slow_generate)
        agent_mock.return_value = agent_client

        coord_client = AsyncMock()
        coord_client.generate = AsyncMock(return_value=SUMMARY)
        coord_mock.return_value = coord_client

        coordinator = CoordinatorAgent()
        coordinator._agent_concurrency = 1
        await coordinator.run(ctx)

    assert in_flight["max"] == 1


@pytest.mark.asyncio
async def test_coordinator_agent_concurrency_1_starts_agents_one_at_a_time():
    """The on_agent_start callback for the next agent must not fire until the
    previous agent has fully finished — otherwise progress logs/UI show all
    agents as 'started' immediately even though their LLM calls are actually
    queued behind each other, which looks identical to the original bug."""
    events: list[tuple[str, str]] = []

    async def slow_generate(system_prompt, user_prompt):
        await asyncio.sleep(0.02)
        return json.dumps([])

    with patch("app.agents.base_agent.get_llm_client") as agent_mock, \
         patch("app.agents.coordinator.get_llm_client") as coord_mock:
        agent_client = AsyncMock()
        agent_client.generate = AsyncMock(side_effect=slow_generate)
        agent_mock.return_value = agent_client

        coord_client = AsyncMock()
        coord_client.generate = AsyncMock(return_value=SUMMARY)
        coord_mock.return_value = coord_client

        coordinator = CoordinatorAgent()
        coordinator._agent_concurrency = 1
        await coordinator.run(
            SAMPLE_CTX,
            on_agent_start=lambda name: events.append(("start", name)),
            on_agent_done=lambda name, n: events.append(("done", name)),
        )

    # Strict alternation: start, done, start, done, ... — never two starts
    # (or two dones) back to back, which would mean agents overlapped.
    kinds = [kind for kind, _ in events]
    assert kinds == ["start", "done"] * (len(kinds) // 2)
