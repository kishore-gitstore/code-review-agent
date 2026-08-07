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
        retry=retry_if_not_exception_type(OllamaTimeoutError),
    )
    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model":   self.model,
            "stream":  False,
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

    async def health_check(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{self.base_url}/api/tags")
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
            reply = await self.generate("You are helpful.", "Reply with exactly: OK")
            return {"status": "ok", "provider": "ollama", "model": self.model, "reply": reply.strip()}
        except Exception as exc:
            return {"status": "error", "provider": "ollama", "model": self.model, "error": str(exc)}
