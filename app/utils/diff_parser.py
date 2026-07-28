"""
app/utils/diff_parser.py — Helpers for working with unified diffs
"""

import re
from dataclasses import dataclass, field


@dataclass
class FileDiff:
    path: str
    old_path: str = ""
    hunks: list[str] = field(default_factory=list)
    added_lines: int = 0
    removed_lines: int = 0

    @property
    def full_diff(self) -> str:
        return "\n".join(self.hunks)


_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")


def parse_diff(raw_diff: str, max_lines: int = 2000) -> list[FileDiff]:
    """
    Split a unified diff into per-file FileDiff objects.
    Truncates each file's diff to max_lines to avoid overwhelming the LLM.
    """
    files: list[FileDiff] = []
    current: FileDiff | None = None

    for line in raw_diff.splitlines():
        # New file header
        if line.startswith("diff --git"):
            if current:
                files.append(current)
            match = _DIFF_HEADER_RE.match(line)
            if match:
                old_path, path = match.group(1), match.group(2)
            else:
                old_path = path = "unknown"
            current = FileDiff(path=path, old_path=old_path)
            continue

        if current is None:
            continue

        if line.startswith("+") and not line.startswith("+++"):
            current.added_lines += 1
        elif line.startswith("-") and not line.startswith("---"):
            current.removed_lines += 1

        current.hunks.append(line)

    if current:
        files.append(current)

    # Truncate large diffs
    for f in files:
        if len(f.hunks) > max_lines:
            f.hunks = f.hunks[:max_lines] + [
                f"... [truncated — {len(f.hunks) - max_lines} lines omitted] ..."
            ]

    return files


def get_changed_files(raw_diff: str) -> list[str]:
    """Return just the list of changed file paths from a diff."""
    return [fd.path for fd in parse_diff(raw_diff)]


def get_path_map(raw_diff: str) -> dict[str, str]:
    """Map new_path -> old_path for every file in the diff (differs only for renames)."""
    return {fd.path: fd.old_path for fd in parse_diff(raw_diff)}
