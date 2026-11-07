"""
Model Provider Abstraction for AURA V4.

Provides a clean interface for LLM interactions with Ollama.
Designed for extensibility to support cloud providers in future phases.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional, Protocol
from dataclasses import dataclass

from ollama import chat
from ollama import ChatResponse


@dataclass
class ModelOptions:
    """Configuration for model invocation."""
    num_ctx: int = 12288
    temperature: float = 0.0
    top_p: float = 0.1
    num_predict: int = 1536
    think: bool = False

    def __post_init__(self):
        env_ctx = os.environ.get("AGENT_NUM_CTX")
        if env_ctx:
            try:
                self.num_ctx = int(env_ctx)
            except ValueError:
                pass

    def to_dict(self) -> dict[str, Any]:
        return {
            "num_ctx": self.num_ctx,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "num_predict": self.num_predict,
        }


class LLMProvider(Protocol):
    """Abstract provider interface for model routing."""
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: ModelOptions | None = None,
    ) -> ChatResponse:
        ...


class OllamaProvider:
    """Local Ollama LLM provider implementation."""

    def __init__(
        self,
        model: str | None = None,
        default_options: ModelOptions | None = None,
    ):
        self.model = model or os.environ.get("AGENT_MODEL", "qwen3.5:4b")
        self.default_options = default_options or ModelOptions()
        self.last_prompt_eval_count: int | None = None
        self.last_eval_count: int | None = None
        self.total_prompt_eval_count: int = 0
        self.total_eval_count: int = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: ModelOptions | None = None,
    ) -> ChatResponse:
        """
        Invoke Ollama chat with tool calling support.

        Args:
            messages: Conversation messages
            tools: Ollama-compatible tool schemas
            options: Model invocation options

        Returns:
            ChatResponse from Ollama

        Raises:
            ConnectionError: If Ollama is unreachable
            ValueError: If model is unavailable or response is malformed
        """
        opts = options or self.default_options
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "options": opts.to_dict(),
        }
        if tools:
            kwargs["tools"] = tools
        if opts.think:
            kwargs["think"] = True

        try:
            # Run in thread pool to avoid blocking event loop
            response = await asyncio.to_thread(chat, **kwargs)
            resp = self._validate_response(response)
            if hasattr(resp, "prompt_eval_count") and resp.prompt_eval_count is not None:
                self.last_prompt_eval_count = resp.prompt_eval_count
                self.total_prompt_eval_count += resp.prompt_eval_count
            if hasattr(resp, "eval_count") and resp.eval_count is not None:
                self.last_eval_count = resp.eval_count
                self.total_eval_count += resp.eval_count
            return resp
        except ConnectionError as e:
            raise ConnectionError(f"Cannot connect to Ollama: {e}") from e
        except Exception as e:
            # Check for model not found
            if "not found" in str(e).lower() or "model" in str(e).lower() and "available" in str(e).lower():
                raise ValueError(f"Model '{self.model}' not available in Ollama: {e}") from e
            raise

    def _validate_response(self, response: Any) -> ChatResponse:
        """Validate that response has expected structure."""
        if response is None:
            raise ValueError("Ollama returned None response")
        if not hasattr(response, 'message'):
            raise ValueError(f"Ollama response missing 'message' attribute: {type(response)}")
        return response

    def is_model_available(self) -> bool:
        """Check if the model is available in Ollama."""
        try:
            import ollama
            models = ollama.list()
            # Model objects have 'model' attribute, not 'name'
            available = []
            for m in models.get('models', []):
                if hasattr(m, 'model'):
                    available.append(m.model)
                elif isinstance(m, dict):
                    available.append(m.get('model', '') or m.get('name', ''))
            return any(self.model in m for m in available)
        except Exception:
            return False


class ProviderFactory:
    """Factory for creating model providers."""

    @staticmethod
    def create_ollama(
        model: str | None = None,
        num_ctx: int | None = None,
        temperature: float | None = None,
        num_predict: int | None = None,
    ) -> OllamaProvider:
        """Create Ollama provider with custom options."""
        opts = ModelOptions()
        if num_ctx is not None:
            opts.num_ctx = num_ctx
        if temperature is not None:
            opts.temperature = temperature
        if num_predict is not None:
            opts.num_predict = num_predict
        return OllamaProvider(model=model, default_options=opts)

    @staticmethod
    def create_from_env() -> OllamaProvider:
        """Create provider using environment variables."""
        return OllamaProvider(
            model=os.environ.get("AGENT_MODEL"),
            default_options=ModelOptions(
                num_ctx=int(os.environ.get("AGENT_NUM_CTX", "12288")),
                temperature=float(os.environ.get("AGENT_TEMPERATURE", "0.0")),
                top_p=float(os.environ.get("AGENT_TOP_P", "0.1")),
                num_predict=int(os.environ.get("AGENT_SYNTHESIS_MAX_TOKENS", "1536")),
            )
        )