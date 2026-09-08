"""
tests/test_reporting.py — Unit tests for app/reporting.py's dashboard read-side
(list_reports/read_report). Focused especially on read_report's path-traversal
guard, since filename there comes straight from an HTTP path parameter in
server.py's /api/reports/{filename} route.
"""

import json
import pytest
from unittest.mock import patch

from app import reporting


def _write_report(tmp_path, name, **overrides):
    data = {"repository": "r", "branch": "main", "status": "success"}
    data.update(overrides)
    (tmp_path / name).write_text(json.dumps(data))
    return data


def test_read_report_returns_file_contents(tmp_path):
    _write_report(tmp_path, "review_local_20260101_000000.json", status="success")
    with patch.object(reporting, "REPORTS_DIR", tmp_path):
        data = reporting.read_report("review_local_20260101_000000.json")
    assert data["status"] == "success"


@pytest.mark.parametrize("filename", [
    "../../etc/passwd",
    "..\\..\\windows\\system32\\config",
    "review_../secret.json",
    "/etc/passwd",
    "not_a_review.json",
    "review_missing_suffix",
    "review_has/slash.json",
])
def test_read_report_rejects_invalid_filenames(tmp_path, filename):
    """Regression guard: filename is attacker-controlled (an HTTP path param),
    so anything that isn't a plain 'review_*.json' basename inside REPORTS_DIR
    must be rejected before the filesystem is ever touched."""
    with patch.object(reporting, "REPORTS_DIR", tmp_path):
        with pytest.raises(ValueError):
            reporting.read_report(filename)


def test_read_report_raises_not_found_for_missing_file(tmp_path):
    with patch.object(reporting, "REPORTS_DIR", tmp_path):
        with pytest.raises(FileNotFoundError):
            reporting.read_report("review_local_99999999_000000.json")


def test_list_reports_empty_when_dir_missing(tmp_path):
    with patch.object(reporting, "REPORTS_DIR", tmp_path / "does_not_exist"):
        assert reporting.list_reports() == []


def test_list_reports_sorted_newest_first(tmp_path):
    import os
    import time

    _write_report(tmp_path, "review_local_1.json", started_at="1")
    time.sleep(0.01)
    _write_report(tmp_path, "review_local_2.json", started_at="2")

    with patch.object(reporting, "REPORTS_DIR", tmp_path):
        reports = reporting.list_reports()

    assert [r["started_at"] for r in reports] == ["2", "1"]
    assert all("_file" in r for r in reports)


def test_list_reports_respects_limit(tmp_path):
    for i in range(5):
        _write_report(tmp_path, f"review_local_{i}.json")

    with patch.object(reporting, "REPORTS_DIR", tmp_path):
        reports = reporting.list_reports(limit=2)

    assert len(reports) == 2


def test_list_reports_skips_unreadable_files(tmp_path):
    _write_report(tmp_path, "review_local_good.json")
    (tmp_path / "review_local_bad.json").write_text("not valid json {{{")

    with patch.object(reporting, "REPORTS_DIR", tmp_path):
        reports = reporting.list_reports()

    assert len(reports) == 1


# ── dashboard payload: summary_dict() contents and list_reports() slimming ────

def _report_with_result():
    from datetime import datetime, timedelta
    from app.utils.models import CodeFix, ReviewComment, ReviewResult, Severity

    report = reporting.ExecutionReport(
        repository="g/r", branch="feature/x", instance="gl", project_id="g/r",
        project_name="g/r", mr_iid=7, mr_title="Add x", mr_author="alice",
        mr_url="https://gitlab.example.com/g/r/-/merge_requests/7",
    )
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    report.on_llm_request("security", "a.py", t0, t0 + timedelta(seconds=2), True)
    report.on_llm_request("security", "b.py", t0, t0 + timedelta(seconds=4), False)
    report.on_llm_request("summary", "-", t0, t0 + timedelta(seconds=1), True)
    result = reporting_result = ReviewResult(mr_iid=7, project_id="g/r", summary="🔴 Request Changes", comments=[
        ReviewComment(agent="security", severity=Severity.critical, file_path="a.py", line=3,
                      title="SQLi", description="d", fix=CodeFix(start_line=3, end_line=3, replacement="x")),
        ReviewComment(agent="code_quality", severity=Severity.low, file_path="b.py", line=None,
                      title="Naming", description="d"),
    ])
    result.compute_counts()
    report.result = result
    report.started_at, report.finished_at, report.status = t0, t0 + timedelta(seconds=9), "success"
    return report


def test_summary_dict_carries_findings_verdict_and_agent_stats():
    d = _report_with_result().summary_dict()
    assert d["summary"] == "🔴 Request Changes"
    assert d["mr_url"].endswith("/merge_requests/7")
    assert d["ai_fixes"] == 1
    assert [c["title"] for c in d["comments"]] == ["SQLi", "Naming"]
    assert d["comments"][0]["fix"] == {"start_line": 3, "end_line": 3, "replacement": "x"}
    assert d["comments"][0]["severity"] == "critical"   # enum serialised as its value, not repr
    assert d["agent_stats"]["security"] == {"requests": 2, "failed": 1, "total_s": 6.0}
    assert d["findings_by_agent"] == {"security": 1, "code_quality": 1}
    assert d["findings_by_file"] == {"a.py": 1, "b.py": 1}
    json.dumps(d)  # save_json() relies on the dict being serialisable as-is


def test_list_reports_is_slim_by_default_but_detail_read_is_full(tmp_path):
    with patch.object(reporting, "REPORTS_DIR", tmp_path):
        path = _report_with_result().save_json()
        [slim] = reporting.list_reports()
        full = reporting.read_report(path.name)
        [unslim] = reporting.list_reports(slim=False)

    assert "comments" not in slim and "llm_requests" not in slim
    assert slim["total_issues"] == 2
    assert slim["agent_stats"]["security"]["requests"] == 2
    assert len(full["comments"]) == 2 and len(full["llm_requests"]) == 3
    assert len(unslim["comments"]) == 2

