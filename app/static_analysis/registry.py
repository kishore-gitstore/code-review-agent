"""
app/static_analysis/registry.py — Plugin registry mapping Language -> analyzer
class. StaticAnalysisRunner reads this instead of hardcoding a dict, so adding
a new analyzer is: write the class, decorate it with @register_analyzer, done
— the runner (the "review engine") never needs to change.

Registration is a decorator applied at class-definition time; app/static_analysis/__init__.py
imports every analyzer module so the decorators actually run before the
runner looks the registry up.
"""

from __future__ import annotations

from .base_analyzer import StaticAnalyzer
from .language_detector import Language

_REGISTRY: dict[Language, type[StaticAnalyzer]] = {}


def register_analyzer(*languages: Language):
    """Class decorator: registers an analyzer class for one or more languages."""
    def _wrap(cls: type[StaticAnalyzer]) -> type[StaticAnalyzer]:
        for lang in languages:
            _REGISTRY[lang] = cls
        return cls
    return _wrap


def registered_analyzers() -> dict[Language, type[StaticAnalyzer]]:
    """Snapshot of the current Language -> analyzer class mapping."""
    return dict(_REGISTRY)
