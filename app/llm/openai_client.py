"""
app/llm/openai_client.py — OpenAI GPT via the Chat Completions API.
Works with GPT-4o, GPT-4.1, o3-mini, o1, etc.
"""

from __future__ import annotations
import asyncio
import httpx
import structlog
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from app.llm.base_client import BaseLLMClient
from app.settings import get_settings

log = structlog.get_logger(__name__)

OPENAI_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIClient(BaseLLMClient):

    def __init__(self):
        cfg = get_settings()
        self.api_key = cfg.openai_api_key
        self.model   = cfg.openai_model

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }

    # retry_if_not_exception_type(CancelledError): tenacity catches
    # BaseException, so without this a review cancelled mid-request would
    # have its LLM call retried instead of stopping.
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=10),
        retry=retry_if_not_exception_type(asyncio.CancelledError),
    )
    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model":       self.model,
            "messages":    [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            "max_tokens":  4096,
            "temperature": 0.1,
        }
        log.debug("openai.request", model=self.model)
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(OPENAI_URL, headers=self._headers(), json=payload)
        if r.status_code == 401:
            raise RuntimeError("OpenAI API key rejected (401). Check OPENAI_API_KEY.")
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    async def health_check(self) -> dict:
        try:
            reply = await self.generate("You are helpful.", "Reply with exactly: OK")
            return {"status": "ok", "provider": "openai", "model": self.model, "reply": reply.strip()}
        except Exception as exc:
            return {"status": "error", "provider": "openai", "model": self.model, "error": str(exc)}
