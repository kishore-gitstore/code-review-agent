"""
app/llm/ollama_client.py — Ollama self-hosted LLM (DeepSeek, LLaMA, Qwen, etc.)
Runs 100% locally — no API keys, no data leaves your machine.
"""

from __future__ import annotations
import asyncio
import time
import httpx
import structlog
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from app.execution.policy import get_execution_policy
from app.llm.base_client import BaseLLMClient
from app.settings import get_settings

log = structlog.get_logger(__name__)


class OllamaTimeoutError(RuntimeError):
    """
    Ollama didn't respond within OLLAMA_TIMEOUT. Unlike a connection error,
    this isn't transient — the model is simply too slow for the configured
    timeout on this hardware — so it's excluded from the retry below and
    raised with an actionable message instead of an opaque RetryError.
    """


class OllamaClient(BaseLLMClient):

    def __init__(self):
        cfg = get_settings()
        self.base_url = cfg.ollama_base_url.rstrip("/")
        self.model    = cfg.ollama_model
        self.timeout  = cfg.ollama_timeout
        self.keep_alive = _keep_alive(cfg.ollama_keep_alive)
        # Most local Ollama setups run one model on one GPU/CPU and process a
        # single generation at a time (this matches Ollama's own default
        # OLLAMA_NUM_PARALLEL=1). The coordinator fires every specialist agent
        # concurrently, so without this gate N requests all hit Ollama at once,
        # queue there, and each one's own client-side timeout clock — already
        # running — expires before Ollama gets to it. Sized from the resolved
        # execution policy (EXECUTION_MODE=cpu forces this to 1 regardless of
        # MAX_PARALLEL_LLM_REQUESTS) — see app/execution/policy.py. Raise it
        # only if your Ollama server actually parallelises requests.
        self._semaphore = asyncio.Semaphore(max(1, get_execution_policy().max_parallel_llm_requests))

    # Retrying a request that timed out because the model is simply slow won't
    # help — it'll just time out again, doubling the wasted wait for a doomed
    # request. So OllamaTimeoutError (raised below) is excluded from retry —
    # it fails fast. Genuinely transient errors (connection refused, DNS
    # blips, a 5xx from raise_for_status) still get one retry.
    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(min=2, max=10),
        # asyncio.CancelledError joins it here for a different reason:
        # tenacity catches BaseException, so a review cancelled mid-request
        # would otherwise have its LLM call retried instead of stopping.
        retry=retry_if_not_exception_type((OllamaTimeoutError, asyncio.CancelledError)),
    )
    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model":   self.model,
            "stream":  False,
            # Explicit, so how long the model stays resident between this
            # review's requests doesn't depend on the Ollama server's own
            # OLLAMA_KEEP_ALIVE. The review runner unloads it outright once
            # the last running review finishes (see unload()).
            "keep_alive": self.keep_alive,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
        }
        queued_at = time.monotonic()
        async with self._semaphore:
            queue_wait_ms = int((time.monotonic() - queued_at) * 1000)
            log.debug("ollama.request", model=self.model, queue_wait_ms=queue_wait_ms)
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as c:
                    r = await c.post(f"{self.base_url}/api/chat", json=payload)
            except httpx.TimeoutException as exc:
                raise OllamaTimeoutError(
                    f"Ollama did not respond within {self.timeout}s (model={self.model}). "
                    "Raise OLLAMA_TIMEOUT and/or use a smaller/faster model for this hardware."
                ) from exc
            r.raise_for_status()
            log.debug("ollama.response", model=self.model, duration_ms=int((time.monotonic() - started) * 1000))
            return r.json()["message"]["content"]

    async def load(self) -> None:
        """Load the model into memory ahead of a review's first request.

        A /api/generate call with no prompt is Ollama's documented "just load
        it" request. Not strictly required — the first chat request would
        load it too — but it starts the (often tens of seconds) load as soon
        as a review needs the model, and the log shows how long it took.
        """
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(f"{self.base_url}/api/generate",
                             json={"model": self.model, "keep_alive": self.keep_alive})
            r.raise_for_status()
        log.info("ollama.model_loaded", model=self.model,
                 duration_ms=int((time.monotonic() - started) * 1000))

    async def unload(self) -> None:
        """Free the model's RAM/VRAM now (keep_alive=0), instead of waiting
        for Ollama's idle timer. Called once no review needs it."""
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{self.base_url}/api/generate",
                             json={"model": self.model, "keep_alive": 0})
            r.raise_for_status()
        log.info("ollama.model_unloaded", model=self.model)

    async def health_check(self) -> dict:
        # Deliberately does NOT run a generation: that would load the model
        # into memory at every service start and on every /health probe,
        # which is exactly the idle load the load/unload-per-review cycle is
        # there to avoid. Reachable server + model pulled is what's checked.
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{self.base_url}/api/tags")
            r.raise_for_status()
            tags  = r.json().get("models", [])
            names = [m["name"] for m in tags]
            found = any(self.model in n for n in names)
            if not found:
                return {
                    "status":  "error",
                    "provider":"ollama",
                    "model":   self.model,
                    "error":   f"Model not found. Run: ollama pull {self.model}",
                    "available": names,
                }
            return {"status": "ok", "provider": "ollama", "model": self.model}
        except Exception as exc:
            return {"status": "error", "provider": "ollama", "model": self.model, "error": str(exc)}


def _keep_alive(raw: str) -> str | int:
    """OLLAMA_KEEP_ALIVE as Ollama wants it: a bare number is seconds (and
    must be sent as a number), anything else is a duration like "10m"."""
    raw = (raw or "").strip() or "10m"
    try:
        return int(raw)
    except ValueError:
        return raw
