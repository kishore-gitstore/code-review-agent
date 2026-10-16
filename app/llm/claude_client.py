"""
app/llm/claude_client.py — Anthropic Claude via the official Messages API.
Requires: pip install anthropic  (already in requirements.txt)
"""

from __future__ import annotations
import asyncio
import httpx
import structlog
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from app.llm.base_client import BaseLLMClient
from app.settings import get_settings

log = structlog.get_logger(__name__)

CLAUDE_URL = "https://api.anthropic.com/v1/messages"


class ClaudeClient(BaseLLMClient):

    def __init__(self):
        cfg = get_settings()
        self.api_key = cfg.anthropic_api_key
        self.model   = cfg.claude_model

    def _headers(self) -> dict:
        return {
            "x-api-key":         self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
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
            "model":      self.model,
            "max_tokens": 4096,
            "system":     system_prompt,
            "messages":   [{"role": "user", "content": user_prompt}],
        }
        log.debug("claude.request", model=self.model)
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(CLAUDE_URL, headers=self._headers(), json=payload)
        if r.status_code == 401:
            raise RuntimeError("Anthropic API key rejected (401). Check ANTHROPIC_API_KEY.")
        r.raise_for_status()
        return r.json()["content"][0]["text"]

    async def health_check(self) -> dict:
        try:
            reply = await self.generate("You are helpful.", "Reply with exactly: OK")
            return {"status": "ok", "provider": "claude", "model": self.model, "reply": reply.strip()}
        except Exception as exc:
            return {"status": "error", "provider": "claude", "model": self.model, "error": str(exc)}
