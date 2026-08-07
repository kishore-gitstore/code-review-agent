"""
app/utils/process_runner.py — Reusable async subprocess runner shared by every
static analyzer (via StaticAnalyzer._run) and by hardware detection (nvidia-smi).

Single place that knows how to: launch a process asynchronously, enforce a
timeout, stay cancellation-safe, capture stdout/stderr/exit code, and log a
structured start/completion pair with duration.
"""

from __future__ import annotations
import asyncio
import time
import structlog
from dataclasses import dataclass

log = structlog.get_logger(__name__)


class ProcessNotFound(Exception):
    """Raised when the target binary isn't installed / on PATH."""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int


async def run_process(
    cmd: list[str],
    *,
    cwd: str | None = None,
    timeout: float,
    tool_name: str | None = None,
) -> ProcessResult:
    """
    Run `cmd`, returning a ProcessResult. Never raises on a non-zero exit —
    most linters/analyzers exit non-zero simply because they found issues.

    Raises ProcessNotFound if the binary isn't on PATH.

    Cancellation-safe: if the awaiting task is cancelled while the process is
    still running (e.g. the whole review got aborted), the child process is
    killed rather than left orphaned — asyncio.wait_for cancelling its inner
    await does NOT by itself terminate the subprocess it was waiting on.
    """
    name = tool_name or cmd[0]
    started = time.monotonic()
    log.debug("process.start", tool=name, cmd=cmd)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ProcessNotFound(f"{name} not found on PATH") from exc

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        duration_ms = int((time.monotonic() - started) * 1000)
        log.debug("process.done", tool=name, exit_code=proc.returncode, duration_ms=duration_ms)
        return ProcessResult(
            returncode=proc.returncode,
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace"),
            timed_out=False,
            duration_ms=duration_ms,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        duration_ms = int((time.monotonic() - started) * 1000)
        log.warning("process.timeout", tool=name, cmd=" ".join(cmd), duration_ms=duration_ms)
        return ProcessResult(returncode=-1, stdout="", stderr="timeout", timed_out=True, duration_ms=duration_ms)
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        log.warning("process.cancelled", tool=name, cmd=" ".join(cmd))
        raise
