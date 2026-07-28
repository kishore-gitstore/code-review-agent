"""
app/llm/base_client.py — Abstract base class all LLM providers must implement.
Adding a new provider = subclass this + register in factory.py.
"""

from abc import ABC, abstractmethod


class BaseLLMClient(ABC):
    """
    All LLM clients expose exactly two methods:
      generate()     — call the model, return text
      health_check() — verify credentials + connectivity
    """

    @abstractmethod
    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        """
        Send system + user messages to the model.
        Returns the assistant reply as a plain string.
        Implementations should retry on transient errors (use tenacity).
        """
        ...

    @abstractmethod
    async def health_check(self) -> dict:
        """
        Verify the provider is reachable and credentials work.
        Returns {"status": "ok"|"error", "model": str, ...}
        """
        ...
