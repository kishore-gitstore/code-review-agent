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
