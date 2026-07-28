"""
tests/test_agents.py — Unit tests for the specialist agents
"""

import pytest
import json
from unittest.mock import AsyncMock, patch

from app.agents.code_quality_agent  import CodeQualityAgent
from app.agents.security_agent      import SecurityAgent
from app.agents.performance_agent   import PerformanceAgent
from app.agents.documentation_agent import DocumentationAgent
from app.agents.base_agent          import BaseAgent
from app.utils.models               import MRContext, Severity

SAMPLE_CTX = MRContext(
    project_id=1,
    mr_iid=42,
    title="Add user login endpoint",
    description="Adds /api/login with email+password",
    author="dev",
    source_branch="feature/login",
    target_branch="main",
    diff="""diff --git a/app/auth.py b/app/auth.py
+def login(email, password):
+    query = f"SELECT * FROM users WHERE email='{email}'"
+    result = db.execute(query)
+    return result
""",
    changed_files=["app/auth.py"],
)

SAMPLE_FINDINGS = [
    {
        "file_path":   "app/auth.py",
        "line":        3,
        "severity":    "critical",
        "title":       "SQL Injection",
        "description": "User input concatenated directly into SQL query.",
        "suggestion":  "Use parameterised queries: db.execute('SELECT * FROM users WHERE email=?', [email])",
    }
]


@pytest.fixture
def mock_ollama():
    """Patch OllamaClient.generate to return a canned JSON response."""
    with patch(
        "app.agents.base_agent.get_llm_client"
    ) as mock_factory:
        client = AsyncMock()
        client.generate = AsyncMock(return_value=json.dumps(SAMPLE_FINDINGS))
        mock_factory.return_value = client
        yield client


# ── BaseAgent parse tests ─────────────────────────────────────────────────────

class ConcreteAgent(BaseAgent):
    name = "test"

    @property
    def system_prompt(self) -> str:
        return "You are a test agent."


def test_parse_clean_json():
    agent = ConcreteAgent()
    result = agent._parse(json.dumps(SAMPLE_FINDINGS))
    assert len(result) == 1
    assert result[0].severity == Severity.critical
    assert result[0].file_path == "app/auth.py"


def test_parse_markdown_fenced():
    agent = ConcreteAgent()
    fenced = f"```json\n{json.dumps(SAMPLE_FINDINGS)}\n```"
    result = agent._parse(fenced)
    assert len(result) == 1


def test_parse_empty_array():
    agent = ConcreteAgent()
    result = agent._parse("[]")
    assert result == []


def test_parse_invalid_json():
    agent = ConcreteAgent()
    result = agent._parse("not json at all")
    assert result == []


# ── Agent integration (mocked LLM) ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_security_agent(mock_ollama):
    agent   = SecurityAgent()
    results = await agent.review(SAMPLE_CTX, SAMPLE_CTX.diff)
    assert len(results) == 1
    assert results[0].agent == "security"
    assert results[0].severity == Severity.critical


@pytest.mark.asyncio
async def test_code_quality_agent(mock_ollama):
    agent   = CodeQualityAgent()
    results = await agent.review(SAMPLE_CTX, SAMPLE_CTX.diff)
    assert len(results) == 1


@pytest.mark.asyncio
async def test_performance_agent(mock_ollama):
    agent   = PerformanceAgent()
    results = await agent.review(SAMPLE_CTX, SAMPLE_CTX.diff)
    assert len(results) == 1


@pytest.mark.asyncio
async def test_documentation_agent(mock_ollama):
    agent   = DocumentationAgent()
    results = await agent.review(SAMPLE_CTX, SAMPLE_CTX.diff)
    assert len(results) == 1
