"""
app/static_analysis — Language detection + a plugin-based static analyzer engine.

Pipeline stage:  Source Code -> Language Detector -> registered analyzer plugin(s)
                 -> Static Analysis Results (list[ReviewComment])

Every analyzer module below registers itself against one or more Language
values via @register_analyzer (see registry.py) as a side effect of being
imported here — StaticAnalysisRunner reads that registry rather than knowing
about any analyzer directly, so adding a new language is: write a new
analyzer module, decorate its class, import it here. Nothing else changes.

Results feed into the LLM prompt (app/agents/base_agent.py) and are merged with
AI findings in app/agents/coordinator.py.
"""

from . import (
    roslyn_analyzer,
    eslint_analyzer,
    ruff_analyzer,
    clippy_analyzer,
    clang_tidy_analyzer,
    spotbugs_analyzer,
)
from .runner import StaticAnalysisRunner
from .language_detector import Language, detect_language
from .diagnostics import DiagnosticIssue

__all__ = ["StaticAnalysisRunner", "Language", "detect_language", "DiagnosticIssue"]
