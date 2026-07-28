"""
app/llm/copilot_client.py — GitHub Copilot Business/Enterprise via GitHub Models API.
Uses your existing Copilot licence. No extra billing.
Endpoint: https://api.githubcopilot.com/chat/completions  (OpenAI-compatible)
"""

from __future__ import annotations
import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from app.llm.base_client import BaseLLMClient
from app.settings import get_settings

log = structlog.get_logger(__name__)

COPILOT_URL = "https://api.githubcopilot.com/chat/completions"


class CopilotClient(BaseLLMClient):

    def __init__(self):
        cfg = get_settings()
        self.token = cfg.github_token
        self.model = cfg.copilot_model

    def _headers(self) -> dict:
        return {
            "Authorization":           f"Bearer {self.token}",
            "Content-Type":            "application/json",
            "Accept":                  "application/json",
            "Editor-Version":          "vscode/1.95.0",
            "Editor-Plugin-Version":   "copilot-chat/0.22.4",
            "Copilot-Integration-Id":  "vscode-chat",
        }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
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
        log.debug("copilot.request", model=self.model)
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(COPILOT_URL, headers=self._headers(), json=payload)
        if r.status_code == 401:
            raise RuntimeError("GitHub token rejected (401). Check GITHUB_TOKEN and Copilot licence.")
        if r.status_code == 403:
            raise RuntimeError("Copilot access forbidden (403). Ensure Copilot Business/Enterprise is enabled.")
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    async def health_check(self) -> dict:
        try:
            reply = await self.generate("You are helpful.", "Reply with exactly: OK")
            return {"status": "ok", "provider": "copilot", "model": self.model, "reply": reply.strip()}
        except Exception as exc:
            return {"status": "error", "provider": "copilot", "model": self.model, "error": str(exc)}
