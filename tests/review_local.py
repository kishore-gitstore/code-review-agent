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
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.coordinator import CoordinatorAgent
from app.utils.models       import MRContext
from app.utils.diff_parser  import get_changed_files


def get_git_diff() -> str:
    result = subprocess.run(
        ["git", "diff", "HEAD~1", "HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("❌ git diff failed:", result.stderr)
        sys.exit(1)
    return result.stdout


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

    ctx = MRContext(
        project_id=0,
        mr_iid=0,
        title="Local review",
        description="",
        author="local",
        source_branch="feature",
        target_branch="main",
        diff=diff,
        changed_files=get_changed_files(diff),
    )

    print(f"🔍 Reviewing {len(ctx.changed_files)} file(s)…")
    coordinator = CoordinatorAgent()
    result      = await coordinator.run(ctx)

    if args.json:
        print(json.dumps(result.model_dump(), indent=2))
        return

    print("\n" + "═" * 60)
    print("📋  REVIEW SUMMARY")
    print("═" * 60)
    print(result.summary)
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


if __name__ == "__main__":
    asyncio.run(main())
