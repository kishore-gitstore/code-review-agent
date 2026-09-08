"""
app/utils/diff_parser.py — Helpers for working with unified diffs
"""

import re
from dataclasses import dataclass, field
from typing import Iterator


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

    @property
    def changed_lines(self) -> set[int]:
        """
        New-file line numbers actually added by this diff (pure context lines
        that merely surround a change are excluded). Used to scope static
        analysis findings — which run against the whole current file, not
        just the diff — down to only what this diff actually touches.
        """
        return {no for no, marker, _ in _walk_new_lines(self.hunks) if marker == "+" and no is not None}

    @property
    def visible_lines(self) -> set[int]:
        """
        Every new-file line number the diff shows (added + unchanged context).
        This is the only code the LLM ever sees for this file, so a proposed
        code fix must stay inside this set — anything outside it would be
        the model guessing at lines it was never shown.
        """
        return visible_new_lines(self.hunks)


_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_TRUNCATION_PREFIX = "... [truncated"


def _walk_new_lines(hunks: list[str]) -> Iterator[tuple[int | None, str, str]]:
    """
    Yield (new_line_no, marker, content) for every line in a per-file diff.

    marker is "+" (added), "-" (removed), " " (unchanged context) or "" for
    lines that aren't code at all (diff/index/---/+++ headers, hunk headers,
    the truncation notice). new_line_no is the line's number in the NEW
    version of the file, or None for removed lines and non-code lines.
    Lines before the first hunk header are treated as headers — this is what
    lets a genuine added line starting with "++" not be mistaken for the
    "+++ b/path" file header.
    """
    new_line: int | None = None  # None until the first @@ header
    for hunk_line in hunks:
        match = _HUNK_HEADER_RE.match(hunk_line)
        if match:
            new_line = int(match.group(1))
            yield None, "", hunk_line
            continue
        if new_line is None or hunk_line.startswith("\\") or hunk_line.startswith(_TRUNCATION_PREFIX):
            yield None, "", hunk_line
            continue
        if hunk_line.startswith("+"):
            yield new_line, "+", hunk_line[1:]
            new_line += 1
        elif hunk_line.startswith("-"):
            yield None, "-", hunk_line[1:]
        else:
            # Context lines carry a leading space; an entirely empty line is
            # an empty context line in some diff producers.
            yield new_line, " ", hunk_line[1:] if hunk_line.startswith(" ") else hunk_line
            new_line += 1


def visible_new_lines(hunks: list[str]) -> set[int]:
    """New-file line numbers present in the diff — added lines plus context."""
    return {no for no, _marker, _ in _walk_new_lines(hunks) if no is not None}


def annotate_line_numbers(diff_text: str) -> str:
    """
    Render a per-file diff with each code line prefixed by its NEW-file line
    number, so an LLM can anchor findings (and code fixes) to exact lines
    instead of counting from hunk headers itself — which models get wrong
    by one surprisingly often. Removed lines have no number in the new file
    and are shown with a blank number column.

        @@ -1,3 +1,4 @@
           1   import os
           2 + import sys
             -     print("hello")
           3 +     print("hello world")
    """
    out: list[str] = []
    for no, marker, content in _walk_new_lines(diff_text.splitlines()):
        if marker == "":
            out.append(content)
        elif no is None:
            out.append(f"{'':>5} {marker} {content}")
        else:
            out.append(f"{no:>5} {marker} {content}")
    return "\n".join(out)


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
                f"{_TRUNCATION_PREFIX} — {len(f.hunks) - max_lines} lines omitted] ..."
            ]

    return files


def get_changed_files(raw_diff: str) -> list[str]:
    """Return just the list of changed file paths from a diff."""
    return [fd.path for fd in parse_diff(raw_diff)]


def get_path_map(raw_diff: str) -> dict[str, str]:
    """Map new_path -> old_path for every file in the diff (differs only for renames)."""
    return {fd.path: fd.old_path for fd in parse_diff(raw_diff)}


def get_changed_line_map(raw_diff: str) -> dict[str, set[int]]:
    """Map file path -> set of new-file line numbers added by the diff."""
    return {fd.path: fd.changed_lines for fd in parse_diff(raw_diff)}
