"""
app/utils/console.py — process-level stdout/stderr encoding.

Almost everything this app prints carries non-ASCII: the ✅/⚠️/🔴 verdict the
summary prompt explicitly asks for, the report tables' rules, em dashes. On
Windows stdout falls back to the ANSI code page (cp1252) whenever it isn't an
interactive UTF-8 console — which is exactly what the NSSM service gets (its
stdout is a redirected log file) and what any `> out.log` gets. Writing an
emoji there raises UnicodeEncodeError from inside a plain print(), so it has
to be settled once for the process instead of guarded at each call site.
"""

from __future__ import annotations
import sys


def force_utf8_streams() -> None:
    """
    Reconfigure stdout/stderr to UTF-8. Idempotent, never raises — call it as
    early as possible from every entry point (main.py, create_app,
    tests/review_local.py).
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # Not a reconfigurable TextIOWrapper: pytest's capture, an already
            # closed handle, an embedded host. Nothing to do — and failing to
            # set up output must never be what takes the process down.
            pass
