"""
app/llm/ollama_client.py — Ollama self-hosted LLM (DeepSeek, LLaMA, Qwen, etc.)
Runs 100% locally — no API keys, no data leaves your machine.
"""

from __future__ import annotations
import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from app.llm.base_client import BaseLLMClient
from app.settings import get_settings

log = structlog.get_logger(__name__)


class OllamaClient(BaseLLMClient):

    def __init__(self):
        cfg = get_settings()
        self.base_url = cfg.ollama_base_url.rstrip("/")
        self.model    = cfg.ollama_model
        self.timeout  = cfg.ollama_timeout

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model":   self.model,
            "stream":  False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
        }
        log.debug("ollama.request", model=self.model)
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(f"{self.base_url}/api/chat", json=payload)
        r.raise_for_status()
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
