"""
tests/test_model_residency.py — the LLM is loaded when a review reaches its
agents and unloaded when the LAST running review finishes, never while
another review still needs it.
"""

import asyncio
import pytest

from app import server
from app.settings import get_settings


async def _settle():
    for _ in range(5):
        pending = [t for t in list(server._detached) if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_overlapping_reviews_load_once_and_unload_after_the_last(_no_real_model_loading, monkeypatch):
    llm = _no_real_model_loading
    monkeypatch.setattr(get_settings(), "ollama_unload_after_review", True)

    await server._acquire_model()          # review A
    await server._acquire_model()          # review B, while A runs
    llm.load.assert_awaited_once()

    server._release_model()                # A finishes
    await _settle()
    llm.unload.assert_not_awaited()        # B still needs it

    server._release_model()                # B finishes
    await _settle()
    llm.unload.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_review_starting_before_the_unload_runs_keeps_the_model(_no_real_model_loading, monkeypatch):
    llm = _no_real_model_loading
    monkeypatch.setattr(get_settings(), "ollama_unload_after_review", True)

    await server._acquire_model()
    server._release_model()                # unload scheduled, not yet run
    await server._acquire_model()          # a new review arrives first
    await _settle()

    llm.unload.assert_not_awaited()
    assert server._model_leases == 1


@pytest.mark.asyncio
async def test_unloading_can_be_switched_off(_no_real_model_loading, monkeypatch):
    llm = _no_real_model_loading
    monkeypatch.setattr(get_settings(), "ollama_unload_after_review", False)

    await server._acquire_model()
    server._release_model()
    await _settle()

    llm.unload.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_load_does_not_fail_the_review(_no_real_model_loading):
    _no_real_model_loading.load.side_effect = RuntimeError("ollama down")
    await server._acquire_model()          # must not raise
    assert server._model_leases == 1
