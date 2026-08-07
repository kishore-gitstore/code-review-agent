"""
tests/test_ollama_client.py — Regression guard for a real production bug:
firing every specialist agent concurrently sent N simultaneous requests to a
single local Ollama instance (which processes one generation at a time by
default), so each request queued behind the others while its own client-side
timeout clock was already running — guaranteeing a timeout under load.
"""

import asyncio
import httpx
import pytest
from unittest.mock import MagicMock, patch

from app.execution.policy import ExecutionPolicy
from app.llm.ollama_client import OllamaClient, OllamaTimeoutError


class _FakeResponse:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"message": {"content": self._content}}


def _make_tracking_async_client(sleep_s: float, in_flight_box: dict):
    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            in_flight_box["current"] += 1
            in_flight_box["max"] = max(in_flight_box["max"], in_flight_box["current"])
            await asyncio.sleep(sleep_s)
            in_flight_box["current"] -= 1
            return _FakeResponse("ok")

    return _FakeAsyncClient


def _make_client(max_concurrency: int) -> OllamaClient:
    cfg = MagicMock(
        ollama_base_url="http://localhost:11434",
        ollama_model="qwen3-coder:latest",
        ollama_timeout=5,
    )
    policy = ExecutionPolicy(mode="gpu", max_parallel_reviews=1,
                              max_parallel_analyzers=1, max_parallel_llm_requests=max_concurrency)
    with patch("app.llm.ollama_client.get_settings", return_value=cfg), \
         patch("app.llm.ollama_client.get_execution_policy", return_value=policy):
        return OllamaClient()


@pytest.mark.asyncio
async def test_generate_serializes_by_default():
    client = _make_client(max_concurrency=1)
    in_flight = {"current": 0, "max": 0}

    with patch("app.llm.ollama_client.httpx.AsyncClient",
               _make_tracking_async_client(0.05, in_flight)):
        await asyncio.gather(*[client.generate("sys", "user") for _ in range(4)])

    assert in_flight["max"] == 1


@pytest.mark.asyncio
async def test_generate_respects_configured_concurrency():
    client = _make_client(max_concurrency=2)
    in_flight = {"current": 0, "max": 0}

    with patch("app.llm.ollama_client.httpx.AsyncClient",
               _make_tracking_async_client(0.05, in_flight)):
        await asyncio.gather(*[client.generate("sys", "user") for _ in range(4)])

    assert in_flight["max"] == 2


def _make_timing_out_async_client(exc: Exception, call_count: dict):
    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            call_count["n"] += 1
            raise exc

    return _FakeAsyncClient


@pytest.mark.asyncio
async def test_generate_fails_fast_on_timeout_without_retrying():
    """
    A read timeout means the model is too slow for OLLAMA_TIMEOUT, not a
    transient blip — retrying identically would just time out again and
    double the wasted wait (this is exactly what happened in production:
    every agent waited out two full timeouts before giving up). Assert it
    raises immediately after a single attempt, wrapped in a clear error.
    """
    client = _make_client(max_concurrency=1)
    call_count = {"n": 0}

    with patch("app.llm.ollama_client.httpx.AsyncClient",
               _make_timing_out_async_client(httpx.ReadTimeout("timed out"), call_count)):
        with pytest.raises(OllamaTimeoutError):
            await client.generate("sys", "user")

    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_generate_still_retries_transient_connection_errors():
    client = _make_client(max_concurrency=1)
    call_count = {"n": 0}

    with patch("app.llm.ollama_client.httpx.AsyncClient",
               _make_timing_out_async_client(httpx.ConnectError("refused"), call_count)):
        with pytest.raises(Exception):
            await client.generate("sys", "user")

    assert call_count["n"] == 2
