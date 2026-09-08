"""
tests/test_code_fixes.py — Applicable code fixes, end to end.

The model returns a "fix" object (line range + replacement) alongside each
finding. BaseAgent._parse_fix validates it into ReviewComment.fix against the
line-numbered diff the model was shown (annotate_line_numbers /
visible_new_lines), and the GitLab client renders it as a ```suggestion block
— which gives the MR author a one-click "Apply suggestion" button that commits
the change, instead of a read-only prose recommendation.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.agents.base_agent import BaseAgent, DIFF_LEGEND, MAX_FIX_LINES
from app.agents.coordinator import CoordinatorAgent
from app.gitlab.client import GitLabClient, _build_inline_body, _build_summary
from app.utils.diff_parser import annotate_line_numbers, parse_diff, visible_new_lines
from app.utils.models import CodeFix, MRContext, ReviewComment, ReviewResult, Severity

# New-file numbering: hunk starts at 10.
#   10   def login(...)            (context)
#        - q = "SELECT ..." + email (removed — no new line number)
#   11 + query = f"SELECT ..."
#   12 + result = db.execute(query)
#   13   return result             (context)
DIFF = """diff --git a/app/auth.py b/app/auth.py
--- a/app/auth.py
+++ b/app/auth.py
@@ -10,4 +10,5 @@ import db
 def login(email, password):
-    q = "SELECT * FROM users WHERE email='" + email + "'"
+    query = f"SELECT * FROM users WHERE email='{email}'"
+    result = db.execute(query)
     return result
"""

CTX = MRContext(
    project_id=1, mr_iid=42, title="Add login", description="", author="dev",
    source_branch="feature/login", target_branch="main",
    diff=DIFF, changed_files=["app/auth.py"],
)

REPLACEMENT = (
    '    query = "SELECT * FROM users WHERE email = ?"\n'
    "    result = db.execute(query, [email])"
)


def _finding(**overrides) -> dict:
    item = {
        "file_path":   "app/auth.py",
        "line":        11,
        "severity":    "critical",
        "title":       "SQL Injection",
        "description": "User input interpolated into the query.",
        "suggestion":  "Use a parameterised query.",
        "fix": {"start_line": 11, "end_line": 12, "replacement": REPLACEMENT + "\n"},
    }
    item.update(overrides)
    return item


class Agent(BaseAgent):
    name = "test"

    @property
    def system_prompt(self) -> str:
        return "test"


def _comment(line=12, fix=CodeFix(start_line=11, end_line=12, replacement=REPLACEMENT), **kw) -> ReviewComment:
    defaults = dict(agent="security", severity=Severity.critical, file_path="app/auth.py",
                    line=line, title="SQL Injection", description="Injectable query.", fix=fix)
    defaults.update(kw)
    return ReviewComment(**defaults)


# ── diff annotation: the line numbers the model anchors fixes to ─────────────

def test_annotate_numbers_new_file_lines_and_marks_removed_ones():
    out = annotate_line_numbers(parse_diff(DIFF)[0].full_diff).splitlines()
    assert "   10   def login(email, password):" in out
    assert "   11 +     query = f\"SELECT * FROM users WHERE email='{email}'\"" in out
    assert "   12 +     result = db.execute(query)" in out
    assert "   13       return result" in out
    # removed line: blank number column, "-" marker
    assert any(l.startswith("      -     q = ") for l in out)


def test_annotate_passes_headers_through_unchanged():
    out = annotate_line_numbers(parse_diff(DIFF)[0].full_diff).splitlines()
    assert "--- a/app/auth.py" in out
    assert "+++ b/app/auth.py" in out          # not mistaken for an added line
    assert "@@ -10,4 +10,5 @@ import db" in out


def test_annotate_is_identity_without_hunk_headers():
    raw = "+def f():\n+    return 1"
    assert annotate_line_numbers(raw) == raw


def test_visible_lines_include_context_changed_lines_do_not():
    fd = parse_diff(DIFF)[0]
    assert fd.visible_lines == {10, 11, 12, 13}
    assert fd.changed_lines == {11, 12}
    assert visible_new_lines(["+no hunk header"]) == set()


# ── CodeFix model ────────────────────────────────────────────────────────────

def test_codefix_rejects_inverted_range():
    with pytest.raises(ValueError):
        CodeFix(start_line=5, end_line=4, replacement="x")


def test_codefix_helpers():
    fix = CodeFix(start_line=11, end_line=12, replacement="x")
    assert fix.line_count == 2
    assert fix.covers(11) and fix.covers(12) and not fix.covers(13)


def test_review_result_counts_fixes():
    result = ReviewResult(mr_iid=1, project_id=1, summary="s",
                          comments=[_comment(), _comment(fix=None)])
    result.compute_counts()
    assert result.fix_count == 1


# ── BaseAgent._parse: validating the model's "fix" ───────────────────────────

VISIBLE = {10, 11, 12, 13}


def test_parse_accepts_valid_fix_and_strips_trailing_newline():
    [c] = Agent()._parse(json.dumps([_finding()]), "app/auth.py", visible_lines=VISIBLE)
    assert c.fix is not None
    assert (c.fix.start_line, c.fix.end_line) == (11, 12)
    assert c.fix.replacement == REPLACEMENT      # trailing "\n" removed
    assert c.suggestion == "Use a parameterised query."
    assert c.line == 11


def test_parse_null_fix_keeps_prose_suggestion():
    [c] = Agent()._parse(json.dumps([_finding(fix=None)]), "app/auth.py", visible_lines=VISIBLE)
    assert c.fix is None
    assert c.suggestion == "Use a parameterised query."


def test_parse_drops_fix_outside_lines_shown_but_keeps_finding():
    item = _finding(fix={"start_line": 11, "end_line": 14, "replacement": "x"})
    [c] = Agent()._parse(json.dumps([item]), "app/auth.py", visible_lines=VISIBLE)
    assert c.fix is None
    assert c.title == "SQL Injection"


def test_parse_drops_fix_whose_range_excludes_reported_line():
    item = _finding(line=11, fix={"start_line": 12, "end_line": 12, "replacement": "x"})
    [c] = Agent()._parse(json.dumps([item]), "app/auth.py", visible_lines=VISIBLE)
    assert c.fix is None
    assert c.line == 11


def test_parse_drops_oversized_fix():
    item = _finding(line=1, fix={"start_line": 1, "end_line": MAX_FIX_LINES + 1, "replacement": "x"})
    [c] = Agent()._parse(json.dumps([item]), "app/auth.py", visible_lines=None)
    assert c.fix is None


@pytest.mark.parametrize("bad_fix", [
    "just a string",
    {"start_line": "eleven", "end_line": 12, "replacement": "x"},
    {"end_line": 12, "replacement": "x"},                       # missing start_line
    {"start_line": 12, "end_line": 11, "replacement": "x"},     # inverted
    {"start_line": 0, "end_line": 0, "replacement": "x"},       # lines are 1-based
])
def test_parse_drops_malformed_fix_but_keeps_finding(bad_fix):
    [c] = Agent()._parse(json.dumps([_finding(fix=bad_fix)]), "app/auth.py", visible_lines=None)
    assert c.fix is None
    assert c.title == "SQL Injection"


def test_parse_anchors_file_level_finding_at_fix_start():
    """A fix is line-anchored by nature; a file-level (line=null) finding that
    carries one gets anchored so it can be posted inline and applied."""
    [c] = Agent()._parse(json.dumps([_finding(line=None)]), "app/auth.py", visible_lines=VISIBLE)
    assert c.fix is not None
    assert c.line == 11


def test_parse_skips_containment_check_when_visible_lines_unknown():
    [c] = Agent()._parse(json.dumps([_finding()]), "app/auth.py", visible_lines=None)
    assert c.fix is not None


@pytest.mark.asyncio
async def test_review_sends_numbered_diff_and_returns_fix():
    with patch("app.agents.base_agent.get_llm_client") as factory:
        client = AsyncMock()
        client.generate = AsyncMock(return_value=json.dumps([_finding()]))
        factory.return_value = client

        fd = parse_diff(DIFF)[0]
        [c] = await Agent().review(CTX, fd.full_diff, file_path=fd.path)

    assert c.fix is not None and c.fix.start_line == 11

    user_prompt = client.generate.await_args.args[1]
    assert DIFF_LEGEND in user_prompt
    assert "   11 +     query = " in user_prompt
    assert '"fix": {' in user_prompt


@pytest.mark.asyncio
async def test_review_drops_fix_that_strays_outside_the_diff():
    item = _finding(fix={"start_line": 11, "end_line": 20, "replacement": "x"})
    with patch("app.agents.base_agent.get_llm_client") as factory:
        client = AsyncMock()
        client.generate = AsyncMock(return_value=json.dumps([item]))
        factory.return_value = client

        fd = parse_diff(DIFF)[0]
        [c] = await Agent().review(CTX, fd.full_diff, file_path=fd.path)

    assert c.fix is None
    assert c.suggestion == "Use a parameterised query."


# ── GitLab rendering ─────────────────────────────────────────────────────────

def test_inline_body_renders_gitlab_suggestion_block():
    body = _build_inline_body(_comment(line=12))
    # comment on line 12, fix covers 11..12 → 1 line above, 0 below
    assert f"```suggestion:-1+0\n{REPLACEMENT}\n```" in body
    assert "Apply suggestion" in body
    assert "lines 11–12" in body


def test_inline_body_single_line_fix_offsets_are_zero():
    fix = CodeFix(start_line=11, end_line=11, replacement="    query = safe(email)")
    body = _build_inline_body(_comment(line=11, fix=fix))
    assert "```suggestion:-0+0\n    query = safe(email)\n```" in body
    assert "line 11" in body


def test_inline_body_offsets_when_comment_is_at_top_of_range():
    body = _build_inline_body(_comment(line=11))
    assert "```suggestion:-0+1\n" in body


def test_inline_body_fix_replaces_prose_suggestion():
    body = _build_inline_body(_comment(line=12, suggestion="Use parameters."))
    assert "suggestion:-1+0" in body
    assert "**Suggestion:**" not in body
    assert "Use parameters." not in body


def test_inline_body_falls_back_to_prose_without_fix():
    body = _build_inline_body(_comment(fix=None, suggestion="Use parameters."))
    assert "**Suggestion:**\n```\nUse parameters.\n```" in body
    assert "suggestion:" not in body


def test_inline_body_readonly_when_apply_disabled():
    body = _build_inline_body(_comment(line=12), applicable=False)
    assert f"```\n{REPLACEMENT}\n```" in body
    assert "suggestion:-" not in body
    assert "Apply suggestion" not in body


def test_inline_body_empty_replacement_means_delete():
    fix = CodeFix(start_line=12, end_line=12, replacement="")
    body = _build_inline_body(_comment(line=12, fix=fix))
    assert "```suggestion:-0+0\n\n```" in body


def test_inline_body_widens_fence_when_replacement_contains_backticks():
    fix = CodeFix(start_line=12, end_line=12, replacement="doc = '''```py\\nx\\n```'''".replace("\\n", "\n"))
    body = _build_inline_body(_comment(line=12, fix=fix))
    assert "````suggestion:-0+0\n" in body
    assert body.rstrip().endswith("````")


def test_inline_body_ignores_fix_not_covering_comment_line():
    """Defensive: _parse_fix already guarantees coverage, but a hand-built
    comment must not produce a negative -N offset GitLab would reject."""
    body = _build_inline_body(_comment(line=13))
    assert "suggestion:" not in body


def test_summary_header_counts_ready_fixes():
    result = ReviewResult(mr_iid=1, project_id=1, summary="s", comments=[_comment(), _comment(fix=None, line=13)])
    result.compute_counts()
    with patch("app.gitlab.client.get_active_model_label", return_value="m"):
        body = _build_summary(result, failed_inline=[])
    assert "🛠️ 1 with a ready-to-apply fix" in body


def test_summary_header_omits_fix_count_when_none():
    result = ReviewResult(mr_iid=1, project_id=1, summary="s", comments=[_comment(fix=None)])
    result.compute_counts()
    with patch("app.gitlab.client.get_active_model_label", return_value="m"):
        body = _build_summary(result, failed_inline=[])
    assert "ready-to-apply" not in body


class _AsyncClientCtx:
    def __init__(self, client):
        self._client = client

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_post_inline_comment_honours_post_fix_suggestions_setting(enabled):
    client = GitLabClient.__new__(GitLabClient)
    client.base_url = "https://gitlab.example.com/api/v4"
    client.headers = {"PRIVATE-TOKEN": "x"}

    response = MagicMock()
    response.raise_for_status = MagicMock()
    http = AsyncMock()
    http.post = AsyncMock(return_value=response)

    settings = MagicMock(post_fix_suggestions=enabled)
    with patch("app.gitlab.client.httpx.AsyncClient", return_value=_AsyncClientCtx(http)), \
         patch("app.gitlab.client.get_settings", return_value=settings):
        ok = await client.post_inline_comment(1, 42, _comment(line=12), "b", "h", "s")

    assert ok is True
    body = http.post.await_args.kwargs["json"]["body"]
    assert ("suggestion:-1+0" in body) is enabled
    assert REPLACEMENT in body


# ── coordinator: dedup must not throw away the copy that has the fix ─────────

def test_dedup_prefers_finding_with_fix_on_equal_severity():
    with patch("app.agents.coordinator.get_settings") as gs, \
         patch("app.agents.coordinator.get_execution_policy") as gp:
        gs.return_value = MagicMock(
            enable_code_quality=False, enable_security=False, enable_performance=False,
            enable_documentation=False, min_severity_to_comment="low", max_diff_lines=2000,
            enable_static_analysis=False,
        )
        gp.return_value = MagicMock(max_parallel_llm_requests=1)
        coordinator = CoordinatorAgent()

    without = _comment(fix=None, agent="code_quality")
    with_fix = _comment(agent="security")

    assert coordinator._dedup([without, with_fix]) == [with_fix]
    assert coordinator._dedup([with_fix, without]) == [with_fix]
