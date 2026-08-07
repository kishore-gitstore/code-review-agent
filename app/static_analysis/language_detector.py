"""
app/static_analysis/language_detector.py — Maps changed file paths to a
programming language so the runner knows which static analyzer to invoke.
"""

from __future__ import annotations
from enum import Enum
from pathlib import PurePosixPath


class Language(str, Enum):
    csharp     = "csharp"
    javascript = "javascript"
    typescript = "typescript"
    python     = "python"
    rust       = "rust"
    cpp        = "cpp"
    java       = "java"


_EXTENSION_MAP: dict[str, Language] = {
    ".cs":  Language.csharp,
    ".js":  Language.javascript,
    ".jsx": Language.javascript,
    ".mjs": Language.javascript,
    ".cjs": Language.javascript,
    ".ts":  Language.typescript,
    ".tsx": Language.typescript,
    ".py":  Language.python,
    ".pyi": Language.python,
    ".rs":  Language.rust,
    ".c":   Language.cpp,
    ".h":   Language.cpp,
    ".cpp": Language.cpp,
    ".cc":  Language.cpp,
    ".cxx": Language.cpp,
    ".hpp": Language.cpp,
    ".hxx": Language.cpp,
    ".java": Language.java,
}


def detect_language(file_path: str) -> Language | None:
    """Return the Language for a file path, or None if not a supported/analyzable type."""
    ext = PurePosixPath(file_path).suffix.lower()
    return _EXTENSION_MAP.get(ext)


def group_by_language(file_paths: list[str]) -> dict[Language, list[str]]:
    """Bucket changed files by detected language, dropping unsupported files."""
    groups: dict[Language, list[str]] = {}
    for path in file_paths:
        lang = detect_language(path)
        if lang is not None:
            groups.setdefault(lang, []).append(path)
    return groups
