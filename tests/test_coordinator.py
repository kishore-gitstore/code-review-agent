"""
tests/test_coordinator.py — Integration tests for the CoordinatorAgent
"""

import pytest
import json
from unittest.mock import AsyncMock, patch

from app.agents.coordinator import CoordinatorAgent
from app.utils.models       import MRContext, Severity

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
