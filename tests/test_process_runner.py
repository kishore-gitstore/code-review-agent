"""
tests/test_process_runner.py — Unit tests for the shared async subprocess
runner used by every static analyzer and by hardware detection.
"""

import asyncio
import pytest
import sys

from app.utils.process_runner import ProcessNotFound, run_process


@pytest.mark.asyncio
async def test_run_process_captures_stdout_stderr_and_exit_code():
    result = await run_process(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
        timeout=10,
    )
    assert result.returncode == 3
    assert "out" in result.stdout
    assert "err" in result.stderr
    assert result.timed_out is False
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_run_process_raises_process_not_found_for_missing_binary():
    with pytest.raises(ProcessNotFound):
        await run_process(["definitely-not-a-real-binary-xyz"], timeout=5)


@pytest.mark.asyncio
async def test_run_process_kills_and_reports_timeout():
    result = await run_process(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        timeout=0.2,
    )
    assert result.timed_out is True
    assert result.returncode == -1


@pytest.mark.asyncio
async def test_run_process_kills_subprocess_on_cancellation():
    """If the awaiting task is cancelled mid-run (e.g. the whole review got
    aborted), the child process must be killed, not left orphaned."""
    task = asyncio.ensure_future(run_process(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        timeout=10,
    ))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
