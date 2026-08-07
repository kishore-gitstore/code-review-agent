#!/usr/bin/env python3
"""
scripts/review_local.py — Review a local git diff without needing GitLab.

Usage:
    # Review HEAD diff
    python scripts/review_local.py

    # Review a specific file's diff
    git diff main...feature/my-branch | python scripts/review_local.py --stdin

    # Review a saved diff file
    python scripts/review_local.py --diff path/to/changes.diff
"""

import asyncio
import sys
import argparse
import subprocess
import json
from datetime import datetime
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.coordinator import CoordinatorAgent
from app.reporting          import ExecutionReport
from app.utils.models       import MRContext
from app.utils.diff_parser  import get_changed_files, parse_diff


def get_git_diff() -> str:
    result = subprocess.run(
        ["git", "diff", "HEAD~1", "HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("❌ git diff failed:", result.stderr)
        sys.exit(1)
    return result.stdout


def get_repo_root() -> str | None:
    """Return the working tree root so static analyzers can run against real files."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def get_current_branch() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


async def main():
    parser = argparse.ArgumentParser(description="Run the code review agent locally")
    parser.add_argument("--diff",  help="Path to a .diff file")
    parser.add_argument("--stdin", action="store_true", help="Read diff from stdin")
    parser.add_argument("--json",  action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if args.stdin:
        diff = sys.stdin.read()
    elif args.diff:
        diff = Path(args.diff).read_text()
    else:
        print("📂 Using: git diff HEAD~1 HEAD")
        diff = get_git_diff()

    if not diff.strip():
        print("⚠️  Empty diff. Nothing to review.")
        sys.exit(0)

    changed_files = get_changed_files(diff)
    file_diffs    = parse_diff(diff)
    repo_root     = get_repo_root()

    ctx = MRContext(
        project_id=0,
        mr_iid=0,
        title="Local review",
        description="",
        author="local",
        source_branch=get_current_branch(),
        target_branch="main",
        diff=diff,
        changed_files=changed_files,
        repo_root=repo_root,
    )

    report = ExecutionReport(
        repository=Path(repo_root).name if repo_root else "N/A",
        branch=ctx.source_branch,
    )
    report.files_changed = len(changed_files)
    report.lines_added   = sum(fd.added_lines for fd in file_diffs)
    report.lines_removed = sum(fd.removed_lines for fd in file_diffs)

    print(f"🔍 Reviewing {len(ctx.changed_files)} file(s)…")
    coordinator = CoordinatorAgent()

    result = None
    report.started_at = datetime.now()
    try:
        result = await coordinator.run(
            ctx,
            on_llm_request=report.on_llm_request,
            on_analyzer_done=report.on_analyzer_done,
            on_phase=report.on_phase,
        )
        report.result = result
        report.status = "success"
    except KeyboardInterrupt:
        report.status = "cancelled"
        report.error  = "interrupted by user"
    except Exception as exc:
        report.status = "failed"
        report.error  = str(exc)
    finally:
        report.finished_at = datetime.now()

    if args.json:
        if result is not None:
            print(json.dumps(result.model_dump(), indent=2))
        else:
            print(json.dumps({"status": report.status, "error": report.error}, indent=2))
    elif result is not None:
        print("\n" + "═" * 60)
        print("📋  REVIEW SUMMARY")
        print("═" * 60)
        print(result.summary)
        print()

        if result.static_analysis_notes:
            print("⚙️  Static analysis:")
            for note in result.static_analysis_notes:
                print(f"   - {note}")
            print()

        if result.comments:
            print(f"Found {result.total_issues} issue(s):\n")
            sev_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}
            for c in result.comments:
                icon = sev_icon.get(c.severity, "⚪")
                loc  = f"{c.file_path}:{c.line}" if c.line else c.file_path
                print(f"{icon} [{c.severity.upper()}] {c.title}")
                print(f"   📍 {loc}  (agent: {c.agent})")
                print(f"   {c.description}")
                if c.suggestion:
                    print(f"   💡 {c.suggestion}")
                print()
        else:
            print("✅ No issues found!")
    else:
        print(f"\n❌ Review did not complete: {report.error}")

    # Requirement: the execution summary prints even on failure/cancellation.
    report.print_console()
    report.log_summary()
    json_path = report.save_json()
    print(f"\n📄 Execution stats saved to {json_path}")

    if report.status != "success":
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⚠️  Cancelled.")
        sys.exit(130)
