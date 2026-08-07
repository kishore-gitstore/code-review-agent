"""
app/utils/models.py — Shared Pydantic models used across agents and the API
"""

from __future__ import annotations
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class Severity(str, Enum):
    low    = "low"
    medium = "medium"
    high   = "high"
    critical = "critical"


class ReviewComment(BaseModel):
    """A single finding produced by one of the specialist agents."""
    agent:       str                   # which agent raised it
    severity:    Severity
    file_path:   str
    line:        Optional[int] = None  # None → file-level comment
    title:       str
    description: str
    suggestion:  Optional[str] = None  # optional code fix


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

    def compute_counts(self) -> None:
        self.total_issues   = len(self.comments)
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
