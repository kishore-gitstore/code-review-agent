"""
app/utils/models.py — Shared Pydantic models used across agents and the API
"""

from __future__ import annotations
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, model_validator


class Severity(str, Enum):
    low    = "low"
    medium = "medium"
    high   = "high"
    critical = "critical"


class CodeFix(BaseModel):
    """
    A concrete, ready-to-apply code change: replace new-file lines
    start_line..end_line (inclusive) with `replacement`. An empty replacement
    deletes those lines. Rendered on GitLab as a ```suggestion block, which
    gives the MR author a one-click "Apply suggestion" button that commits
    the change — as opposed to `ReviewComment.suggestion`, which is prose.
    """
    start_line:  int = Field(ge=1)
    end_line:    int = Field(ge=1)
    replacement: str

    @model_validator(mode="after")
    def _range_is_ordered(self) -> "CodeFix":
        if self.end_line < self.start_line:
            raise ValueError(f"end_line ({self.end_line}) < start_line ({self.start_line})")
        return self

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1

    def covers(self, line: int) -> bool:
        return self.start_line <= line <= self.end_line


class ReviewComment(BaseModel):
    """A single finding produced by one of the specialist agents."""
    agent:       str                   # which agent raised it
    severity:    Severity
    file_path:   str
    line:        Optional[int] = None  # None → file-level comment
    title:       str
    description: str
    suggestion:  Optional[str] = None  # prose recommendation (free text)
    fix:         Optional[CodeFix] = None  # applicable code change, when the model could produce one


class ReviewResult(BaseModel):
    """Aggregated output from the coordinator after all agents finish."""
    mr_iid:          int
    project_id:      str | int
    summary:         str
    comments:        list[ReviewComment] = Field(default_factory=list)
    static_analysis_notes: list[str] = Field(default_factory=list)  # why a language was skipped, if any
    total_issues:    int = 0
    critical_count:  int = 0
    high_count:      int = 0
    medium_count:    int = 0
    low_count:       int = 0
    fix_count:       int = 0  # comments carrying an applicable CodeFix

    # ── review completeness ───────────────────────────────────────────────
    # An agent whose LLM call fails contributes zero findings — BaseAgent.review
    # deliberately swallows the error so one broken agent can't sink the whole
    # review — which is indistinguishable from "found nothing" unless counted.
    # Without these an LLM outage produced an empty comments list and every
    # downstream surface reported a clean, mergeable MR. See `degraded`.
    agent_requests:  int = 0    # agent LLM calls attempted
    agent_failures:  int = 0    # ...of which raised
    summary_failed:  bool = False   # the summary LLM call itself failed

    @property
    def degraded(self) -> bool:
        """True when part of the review never actually ran, so the findings
        are incomplete and the absence of a finding proves nothing."""
        return self.agent_failures > 0 or self.summary_failed

    def compute_counts(self) -> None:
        self.total_issues   = len(self.comments)
        self.fix_count      = sum(1 for c in self.comments if c.fix is not None)
        self.critical_count = sum(1 for c in self.comments if c.severity == Severity.critical)
        self.high_count     = sum(1 for c in self.comments if c.severity == Severity.high)
        self.medium_count   = sum(1 for c in self.comments if c.severity == Severity.medium)
        self.low_count      = sum(1 for c in self.comments if c.severity == Severity.low)


class MRContext(BaseModel):
    """Everything the agents need to know about a Merge Request."""
    project_id:    str | int
    mr_iid:        int
    title:         str
    description:   str
    author:        str
    source_branch: str
    target_branch: str
    diff:          str          # raw unified diff text
    changed_files: list[str]    # list of file paths changed
    repo_root:     Optional[str] = None  # local checkout root, enables static analysis
    repo_root_note: Optional[str] = None  # why repo_root is None, if it is (surfaced to the user)
