"""
Model Provider Abstraction for AURA V4.

Provides a clean interface for LLM interactions with Ollama.
Designed for extensibility to support cloud providers in future phases.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Optional, Protocol
from urllib.parse import urlparse
from dataclasses import dataclass

import httpx
import ollama
from ollama import chat
from ollama import ChatResponse, Message


class CloudToolCall(Message.ToolCall):
    """ToolCall preserving OpenAI/Google metadata (id, thought_signature, extra_content) across turns."""

    id: str | None = None
    extra_content: dict[str, Any] | None = None
    thought_signature: str | None = None
    provider_specific_fields: dict[str, Any] | None = None


def get_ollama_base_url() -> str:
    """Return normalized base URL for configured Ollama host."""
    raw = (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").strip().rstrip("/")
    if not raw.startswith(("http://", "https://")):
        raw = f"http://{raw}"
    return raw


def _is_local_ollama_host(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1", ""}


def is_ollama_running(timeout: float = 0.8) -> bool:
    """Return True if Ollama HTTP API responds on /api/tags."""
    base = get_ollama_base_url()
    try:
        resp = httpx.get(f"{base}/api/tags", timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


def ensure_ollama_running(timeout: float = 10.0) -> bool:
    """Ensure local Ollama daemon is listening; start `ollama serve` on demand if installed."""
    if is_ollama_running(timeout=0.8):
        return True
    if os.environ.get("AURA_NO_AUTO_OLLAMA") == "1":
        return False
    base = get_ollama_base_url()
    if not _is_local_ollama_host(base):
        return False
    ollama_exe = shutil.which("ollama")
    if not ollama_exe:
        return False

    from .mcp_hub import win32_subprocess_kwargs

    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs.update(win32_subprocess_kwargs(detached=True, hide_window=True))
    else:
        kwargs["start_new_session"] = True

    try:
        subprocess.Popen([ollama_exe, "serve"], **kwargs)
    except Exception:
        return False

    deadline = time.monotonic() + max(1.0, float(timeout))
    while time.monotonic() < deadline:
        if is_ollama_running(timeout=0.6):
            return True
        time.sleep(0.25)
    return is_ollama_running(timeout=0.6)


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
        timeout: float | None = None,
    ):
        self.model = model or os.environ.get("AGENT_MODEL", "qwen3.5:4b")
        self.default_options = default_options or ModelOptions()
        if timeout is not None:
            self.timeout = timeout
        elif "AGENT_MODEL_TIMEOUT" in os.environ:
            self.timeout = float(os.environ["AGENT_MODEL_TIMEOUT"])
        else:
            # Adaptive timeout based on model size to prevent false timeouts on laptops
            m_lower = self.model.lower()
            if any(k in m_lower for k in ("9b", "8b", "7b", "14b", "coder")):
                self.timeout = 180.0
            else:
                self.timeout = 120.0
        self._client = ollama.Client(timeout=self.timeout)
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
            "think": bool(opts.think),
        }
        if tools:
            kwargs["tools"] = tools

        try:
            # Execute chat via client configured with hard HTTP transport timeout
            response = await asyncio.to_thread(self._client.chat, **kwargs)
            resp = self._validate_response(response)
            if hasattr(resp, "prompt_eval_count") and resp.prompt_eval_count is not None:
                self.last_prompt_eval_count = resp.prompt_eval_count
                self.total_prompt_eval_count += resp.prompt_eval_count
            if hasattr(resp, "eval_count") and resp.eval_count is not None:
                self.last_eval_count = resp.eval_count
                self.total_eval_count += resp.eval_count
            return resp
        except (httpx.TimeoutException, TimeoutError) as e:
            # Graceful retry with extended timeout to absorb model reload latency
            try:
                extended_client = ollama.Client(timeout=self.timeout + 60.0)
                response = await asyncio.to_thread(extended_client.chat, **kwargs)
                resp = self._validate_response(response)
                if hasattr(resp, "prompt_eval_count") and resp.prompt_eval_count is not None:
                    self.last_prompt_eval_count = resp.prompt_eval_count
                    self.total_prompt_eval_count += resp.prompt_eval_count
                if hasattr(resp, "eval_count") and resp.eval_count is not None:
                    self.last_eval_count = resp.eval_count
                    self.total_eval_count += resp.eval_count
                return resp
            except Exception:
                raise TimeoutError(f"Model call timed out after {self.timeout}s: {e}") from e
        except (ConnectionError, httpx.ConnectError, httpx.NetworkError) as e:
            if not os.environ.get("PYTEST_CURRENT_TEST") and await asyncio.to_thread(ensure_ollama_running, 10.0):
                try:
                    response = await asyncio.to_thread(self._client.chat, **kwargs)
                    resp = self._validate_response(response)
                    if hasattr(resp, "prompt_eval_count") and resp.prompt_eval_count is not None:
                        self.last_prompt_eval_count = resp.prompt_eval_count
                        self.total_prompt_eval_count += resp.prompt_eval_count
                    if hasattr(resp, "eval_count") and resp.eval_count is not None:
                        self.last_eval_count = resp.eval_count
                        self.total_eval_count += resp.eval_count
                    return resp
                except Exception:
                    pass
            raise ConnectionError(f"Cannot connect to Ollama: {e}") from e
        except Exception as e:
            if "think" in str(e).lower() and "support" in str(e).lower():
                kwargs.pop("think", None)
                response = await asyncio.to_thread(self._client.chat, **kwargs)
                return self._validate_response(response)
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
            models = self._client.list()
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

    def unload(self) -> bool:
        """Evict model from GPU memory to allow sequential model switching."""
        success = False
        conn_offline = False
        try:
            # 1. Standard Ollama API to evict model: generate with keep_alive=0
            self._client.generate(model=self.model, keep_alive=0)
            success = True
        except Exception as exc:
            err_msg = str(exc).lower()
            if isinstance(exc, (ConnectionError, OSError)) or any(
                k in err_msg for k in ("connect", "refused", "failed to establish", "timed out")
            ):
                conn_offline = True

        # Skip spawning slow CLI subprocess if Ollama daemon is unreachable or disabled in env/tests.
        if conn_offline or os.environ.get("AURA_NO_AUTO_OLLAMA") == "1":
            return success

        # 2. Stop command to ensure 100% VRAM release
        try:
            from .mcp_hub import win32_subprocess_kwargs

            extra = win32_subprocess_kwargs(hide_window=True)
            res = subprocess.run(["ollama", "stop", self.model], capture_output=True, text=True, timeout=10, **extra)
            if res.returncode == 0:
                success = True
        except Exception:
            pass
        return success


CLOUD_PRESETS: dict[str, dict[str, Any]] = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "model_envs": ("GROQ_MODEL",),
        "key_envs": ("GROQ_API_KEY",),
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-3.8-flash",
        "model_envs": ("GEMINI_MODEL", "AURA_GEMINI_MODEL"),
        "fallback_models": (
            "gemini-3.8-flash",
            "gemini-3-flash-preview",
            "gemini-3.6-flash",
            "gemini-3.5-flash",
        ),
        "key_envs": ("GEMINI_API_KEY", "GEMINIAPI_KEY", "GOOGLE_API_KEY"),
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "qwen/qwen3-coder:free",
        "model_envs": ("OPENROUTER_MODEL",),
        "key_envs": ("OPENROUTER_API_KEY",),
    },
    "github": {
        "base_url": "https://models.inference.ai.azure.com",
        "default_model": "gpt-4o-mini",
        "model_envs": ("GITHUB_MODEL",),
        "key_envs": ("GITHUB_TOKEN",),
    },
}


def _first_env(*names: str) -> str:
    for name in names:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return ""


def _preset_default_model(name: str) -> str:
    cfg = CLOUD_PRESETS[name]
    model_envs = cfg.get("model_envs") or ()
    return _first_env(*model_envs) or str(cfg["default_model"])


def _is_freellmapi_url(base_url: str) -> bool:
    parsed = urlparse((base_url or "").strip())
    host = (parsed.hostname or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1"} and parsed.port == 31415


def is_freellmapi_running(base_url: str, timeout: float = 0.8) -> bool:
    """Return True if local FreeLLMAPI gateway responds on /models."""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return False
    if url.lower().endswith("/chat/completions"):
        url = url[: -len("/chat/completions")].rstrip("/")
    try:
        resp = httpx.get(f"{url}/models", timeout=timeout)
        return resp.status_code < 500
    except Exception:
        return False


def ensure_freellmapi_running(base_url: str, timeout: float = 8.0) -> bool:
    """Launch FreeLLMAPI desktop gateway on demand when configured on localhost:31415."""
    if not _is_freellmapi_url(base_url):
        return False
    if is_freellmapi_running(base_url, timeout=0.8):
        return True
    if os.environ.get("AURA_NO_AUTO_FREELMAPI") == "1" or os.environ.get("PYTEST_CURRENT_TEST"):
        return False

    candidates: list[Path] = []
    env_exe = os.environ.get("FREELLMAPI_EXE", "").strip()
    if env_exe:
        candidates.append(Path(env_exe).expanduser())
    candidates.extend([
        Path.home() / "AppData" / "Local" / "Programs" / "freellmapi-desktop" / "FreeLLMAPI.exe",
    ])
    which_exe = shutil.which("FreeLLMAPI") or shutil.which("freellmapi")
    if which_exe:
        candidates.append(Path(which_exe))

    exe_path = next((p for p in candidates if p.is_file()), None)
    if exe_path is None:
        return False

    from .mcp_hub import win32_subprocess_kwargs

    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs.update(win32_subprocess_kwargs(detached=True, hide_window=True))
    else:
        kwargs["start_new_session"] = True

    try:
        subprocess.Popen([str(exe_path)], **kwargs)
    except Exception:
        return False

    deadline = time.monotonic() + max(1.0, float(timeout))
    while time.monotonic() < deadline:
        if is_freellmapi_running(base_url, timeout=0.8):
            return True
        time.sleep(0.35)
    return is_freellmapi_running(base_url, timeout=0.8)


def is_cloud_model_spec(model: str | None) -> bool:
    """Return True when *model* selects a cloud API target."""
    if not isinstance(model, str) or not model:
        return False
    lower = model.strip().lower()
    return lower == "cloud" or lower.startswith("cloud:")


def _resolve_custom_candidate(model_override: str = "") -> tuple[str, str, str, str] | None:
    custom_url = _first_env("AURA_CLOUD_BASE_URL", "FREELLMAPI_ENDPOINT", "OPENAI_BASE_URL")
    custom_key = _first_env("AURA_CLOUD_API_KEY", "FREELLMAPI_KEY", "OPENAI_API_KEY")
    custom_model = model_override or _first_env("AURA_CLOUD_MODEL", "FREELLMAPI_MODEL") or "auto"
    if custom_url and custom_key:
        return ("custom", custom_url.rstrip("/"), custom_key, custom_model)
    return None


def resolve_cloud_candidates(spec: str | None = None) -> list[tuple[str, str, str, str]]:
    """Return ordered configured *(preset, base_url, api_key, api_model)* candidates for failover."""
    from ..config.settings import load_aura_env
    load_aura_env()
    raw = (spec or "").strip()
    if raw.lower().startswith("cloud:"):
        raw = raw[6:].strip()
    elif raw.lower() == "cloud":
        raw = ""

    explicit_preset = ""
    model_override = ""
    if raw and raw.lower() != "auto":
        if "/" in raw:
            head, tail = raw.split("/", 1)
            if head.lower() in CLOUD_PRESETS or head.lower() == "custom":
                explicit_preset = head.lower()
                model_override = "" if tail.lower() == "auto" else tail
            else:
                model_override = raw
        elif raw.lower() in CLOUD_PRESETS:
            explicit_preset = raw.lower()
        elif raw.lower() == "custom":
            explicit_preset = "custom"
        else:
            model_override = raw

    raw_order = os.environ.get("AURA_CLOUD_PRIORITY", "gemini,custom,groq,openrouter,github")
    order = [p.strip().lower() for p in raw_order.split(",") if p.strip()]
    for name in ("gemini", "custom", *CLOUD_PRESETS.keys()):
        if name not in order:
            order.append(name)

    if explicit_preset:
        order = [explicit_preset] + [p for p in order if p != explicit_preset]

    candidates: list[tuple[str, str, str, str]] = []
    for name in order:
        if name == "custom":
            cand = _resolve_custom_candidate(model_override if explicit_preset == "custom" else "")
            if cand is not None:
                candidates.append(cand)
        elif name in CLOUD_PRESETS:
            cfg = CLOUD_PRESETS[name]
            key = _first_env(*cfg["key_envs"])
            if not key and explicit_preset == name:
                key = _first_env("AURA_CLOUD_API_KEY", "FREELLMAPI_KEY")
            if key:
                mdl = (model_override if explicit_preset == name else "") or _preset_default_model(name)
                candidates.append((name, cfg["base_url"], key, mdl))
                if explicit_preset == name:
                    for fb_mdl in cfg.get("fallback_models") or ():
                        if fb_mdl != mdl:
                            candidates.append((name, cfg["base_url"], key, str(fb_mdl)))
    return candidates


def resolve_cloud_target(spec: str | None = None) -> tuple[str, str, str, str]:
    """Resolve primary *(preset, base_url, api_key, api_model)* from spec and environment."""
    from ..config.settings import load_aura_env
    load_aura_env()
    raw = (spec or "").strip()
    if raw.lower().startswith("cloud:"):
        raw = raw[6:].strip()
    elif raw.lower() == "cloud":
        raw = ""

    preset = ""
    model_override = ""
    if raw and raw.lower() != "auto":
        if "/" in raw:
            head, tail = raw.split("/", 1)
            if head.lower() in CLOUD_PRESETS or head.lower() == "custom":
                preset = head.lower()
                model_override = "" if tail.lower() == "auto" else tail
            else:
                model_override = raw
        elif raw.lower() in CLOUD_PRESETS:
            preset = raw.lower()
        elif raw.lower() == "custom":
            preset = "custom"
        else:
            model_override = raw

    if preset in CLOUD_PRESETS:
        cfg = CLOUD_PRESETS[preset]
        return (
            preset,
            cfg["base_url"],
            _first_env(*cfg["key_envs"], "AURA_CLOUD_API_KEY", "FREELLMAPI_KEY"),
            model_override or _preset_default_model(preset),
        )

    custom_url = _first_env("AURA_CLOUD_BASE_URL", "FREELLMAPI_ENDPOINT", "OPENAI_BASE_URL")
    custom_key = _first_env("AURA_CLOUD_API_KEY", "FREELLMAPI_KEY", "OPENAI_API_KEY")
    custom_model = model_override or _first_env("AURA_CLOUD_MODEL", "FREELLMAPI_MODEL") or "auto"
    if preset == "custom":
        return ("custom", custom_url.rstrip("/"), custom_key, custom_model)

    candidates = resolve_cloud_candidates(spec)
    if candidates:
        return candidates[0]

    return ("unconfigured", custom_url.rstrip("/"), custom_key, custom_model)


def _to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Ollama/internal message list into OpenAI chat-completions schema."""
    converted: list[dict[str, Any]] = []
    pending_tool_ids: list[str] = []
    call_seq = 0

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content") or ""
        if role == "assistant" and msg.get("tool_calls"):
            openai_calls = []
            pending_tool_ids = []
            for tc in msg["tool_calls"]:
                is_dict = isinstance(tc, dict)
                func = tc.get("function") if is_dict else getattr(tc, "function", None)
                if not func:
                    continue
                name = func.get("name") if isinstance(func, dict) else getattr(func, "name", "")
                args = func.get("arguments", {}) if isinstance(func, dict) else getattr(func, "arguments", {})
                args_str = args if isinstance(args, str) else json.dumps(args or {})
                call_seq += 1
                raw_id = tc.get("id") if is_dict else getattr(tc, "id", None)
                tc_id = str(raw_id) if raw_id else f"call_{call_seq}"
                pending_tool_ids.append(tc_id)
                call_entry: dict[str, Any] = {
                    "id": tc_id,
                    "type": "function",
                    "function": {"name": str(name), "arguments": args_str},
                }
                for meta_key in ("extra_content", "thought_signature", "provider_specific_fields"):
                    meta_val = tc.get(meta_key) if is_dict else getattr(tc, meta_key, None)
                    if meta_val is not None:
                        call_entry[meta_key] = meta_val
                openai_calls.append(call_entry)
            entry: dict[str, Any] = {"role": "assistant", "content": content or None}
            if openai_calls:
                entry["tool_calls"] = openai_calls
            converted.append(entry)
        elif role == "tool":
            tc_id = msg.get("tool_call_id") or (pending_tool_ids.pop(0) if pending_tool_ids else f"call_{call_seq + 1}")
            converted.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": str(content),
            })
        else:
            pending_tool_ids = []
            converted.append({"role": role, "content": str(content)})
    return converted


_CLOUD_COOLDOWN_UNTIL: dict[tuple[str, str], float] = {}
_CLOUD_COOLDOWN_SECONDS = 120.0


def clear_cloud_cooldowns() -> None:
    """Clear process-wide cloud endpoint/model cooldowns."""
    _CLOUD_COOLDOWN_UNTIL.clear()


class CloudProvider:
    """OpenAI-compatible REST provider for free/external cloud LLM APIs with automatic failover."""

    def __init__(
        self,
        model: str = "cloud:auto",
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        default_options: ModelOptions | None = None,
        timeout: float = 90.0,
        auto_failover: bool | None = None,
    ) -> None:
        preset, resolved_url, resolved_key, resolved_api_model = resolve_cloud_target(model)
        self.preset = preset
        clean_url = (base_url or resolved_url).strip().rstrip("/")
        if clean_url.lower().endswith("/chat/completions"):
            clean_url = clean_url[: -len("/chat/completions")].rstrip("/")
        self.base_url = clean_url
        self.api_key = api_key if api_key is not None else resolved_key
        self.api_model = resolved_api_model
        self.model = f"cloud:{preset}/{self.api_model}" if preset != "unconfigured" else (model or "cloud:auto")
        self.default_options = default_options or ModelOptions()
        self.timeout = timeout
        self.auto_failover = (api_key is None and base_url is None) if auto_failover is None else auto_failover
        self._candidates: list[tuple[str, str, str, str]] = (
            resolve_cloud_candidates(model) if self.auto_failover else []
        )
        if not self._candidates and self.base_url and self.api_key:
            self._candidates = [(self.preset, self.base_url, self.api_key, self.api_model)]
        self._active_idx: int = 0
        self.failover_events: list[str] = []
        self.last_prompt_eval_count: int | None = None
        self.last_eval_count: int | None = None
        self.total_prompt_eval_count: int = 0
        self.total_eval_count: int = 0

    def is_model_available(self) -> bool:
        return bool(self.base_url and self.api_key)

    def unload(self) -> bool:
        return True

    def _activate_candidate(self, idx: int) -> None:
        preset, base_url, api_key, api_model = self._candidates[idx]
        self._active_idx = idx
        self.preset = preset
        self.base_url = base_url.strip().rstrip("/")
        self.api_key = api_key
        self.api_model = api_model
        self.model = f"cloud:{preset}/{api_model}"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: ModelOptions | None = None,
    ) -> ChatResponse:
        if not self.base_url or not self.api_key:
            raise ValueError(
                "No free cloud LLM API key configured. Set GEMINI_API_KEY, GROQ_API_KEY, "
                "OPENROUTER_API_KEY, GITHUB_TOKEN, or FREELLMAPI_ENDPOINT + FREELLMAPI_KEY in .env."
            )

        opts = options or self.default_options
        oai_messages = _to_openai_messages(messages)
        raw_candidates = list(self._candidates) or [(self.preset, self.base_url, self.api_key, self.api_model)]
        candidates = list(raw_candidates[min(self._active_idx, len(raw_candidates) - 1) :])
        if not os.environ.get("PYTEST_CURRENT_TEST") and len(candidates) > 1:
            now_mono = time.monotonic()
            # Keep Gemini primary in cloud:auto by promoting any healthy sibling Gemini fallback before bumping FreeLLMAPI ahead.
            promoted: list[tuple[str, str, str, str]] = []
            for p_name, b_url, a_key, a_mdl in candidates:
                if _CLOUD_COOLDOWN_UNTIL.get((p_name, a_mdl), 0.0) > now_mono:
                    siblings = CLOUD_PRESETS.get(p_name, {}).get("fallback_models") or ()
                    healthy_sib = next((s for s in siblings if s != a_mdl and _CLOUD_COOLDOWN_UNTIL.get((p_name, s), 0.0) <= now_mono), None)
                    if healthy_sib:
                        a_mdl = str(healthy_sib)
                promoted.append((p_name, b_url, a_key, a_mdl))
            candidates = promoted
            healthy = [c for c in candidates if _CLOUD_COOLDOWN_UNTIL.get((c[0], c[3]), 0.0) <= now_mono]
            if healthy:
                cooled = [c for c in candidates if c not in healthy]
                candidates = healthy + cooled
        safe_max_tokens = min(max(1, int(opts.num_predict or 1536)), 8192)

        def _mark_cooldown(p_name: str, m_name: str) -> None:
            if not os.environ.get("PYTEST_CURRENT_TEST"):
                _CLOUD_COOLDOWN_UNTIL[(p_name, m_name)] = time.monotonic() + _CLOUD_COOLDOWN_SECONDS

        data: dict[str, Any] | None = None
        last_exc: Exception | None = None

        idx = 0
        gemini_fb_injected = False
        while idx < len(candidates):
            preset, base_url, api_key, api_model = candidates[idx]
            if _is_freellmapi_url(base_url) and not is_freellmapi_running(base_url, timeout=0.6):
                await asyncio.to_thread(ensure_freellmapi_running, base_url, 8.0)

            payload: dict[str, Any] = {
                "model": api_model,
                "messages": oai_messages,
                "temperature": opts.temperature,
                "top_p": opts.top_p,
                "max_tokens": safe_max_tokens,
            }
            if preset == "gemini" and api_model.startswith("gemini-3") and not os.environ.get("PYTEST_CURRENT_TEST"):
                payload["reasoning_effort"] = "minimal" if api_model == "gemini-3-flash-preview" else "low"
            if tools:
                payload["tools"] = tools

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            url = f"{base_url.rstrip('/')}/chat/completions"

            def _maybe_inject_gemini_fallback() -> None:
                nonlocal gemini_fb_injected
                if not gemini_fb_injected and idx + 1 >= len(candidates) and preset == "gemini":
                    gemini_fb_injected = True
                    for fb in ("gemini-3-flash-preview", "gemini-3.6-flash"):
                        if fb != api_model:
                            candidates.append(("gemini", base_url, api_key, fb))

            has_fallback = (idx + 1 < len(candidates)) or (
                not gemini_fb_injected and preset == "gemini" and api_model not in ("gemini-3.6-flash",)
            )
            fb_cap = 55.0 if _is_freellmapi_url(base_url) else 12.0
            req_timeout: Any = (
                httpx.Timeout(min(float(self.timeout), fb_cap), connect=5.0)
                if has_fallback
                else self.timeout
            )

            try:
                async with httpx.AsyncClient(timeout=req_timeout) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code == 400 and "reasoning_effort" in payload:
                        payload.pop("reasoning_effort", None)
                        resp = await client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                last_exc = TimeoutError(f"Cloud API request timed out after {self.timeout}s: {exc}")
                _mark_cooldown(preset, api_model)
                _maybe_inject_gemini_fallback()
                if idx + 1 < len(candidates):
                    self.failover_events.append(f"{preset}->timeout")
                    nxt = candidates[idx + 1]
                    if nxt in self._candidates:
                        self._activate_candidate(self._candidates.index(nxt))
                    idx += 1
                    continue
                raise last_exc from exc
            except (httpx.ConnectError, httpx.NetworkError) as exc:
                if _is_freellmapi_url(base_url) and await asyncio.to_thread(ensure_freellmapi_running, base_url, 8.0):
                    try:
                        async with httpx.AsyncClient(timeout=self.timeout) as client:
                            resp = await client.post(url, json=payload, headers=headers)
                    except Exception:
                        resp = None
                else:
                    resp = None
                if resp is None:
                    hint = (
                        " (ensure FreeLLMAPI desktop is running on port 31415)"
                        if _is_freellmapi_url(base_url)
                        else ""
                    )
                    last_exc = ConnectionError(f"Cannot connect to cloud LLM API ({base_url}){hint}: {exc}")
                    _mark_cooldown(preset, api_model)
                    _maybe_inject_gemini_fallback()
                    if idx + 1 < len(candidates):
                        self.failover_events.append(f"{preset}->connect_error")
                        nxt = candidates[idx + 1]
                        if nxt in self._candidates:
                            self._activate_candidate(self._candidates.index(nxt))
                        idx += 1
                        continue
                    raise last_exc from exc

            if resp.status_code in (404, 413, 429) or resp.status_code >= 500:
                if resp.status_code == 429:
                    last_exc = TimeoutError(f"Cloud API rate limit reached (429) for 'cloud:{preset}/{api_model}'.")
                elif resp.status_code == 404:
                    last_exc = ValueError(
                        f"Cloud API rejected request (404) for 'cloud:{preset}/{api_model}': {resp.text[:200]}"
                    )
                else:
                    last_exc = RuntimeError(f"Cloud API error ({resp.status_code}): {resp.text[:240]}")
                _mark_cooldown(preset, api_model)
                _maybe_inject_gemini_fallback()
                if idx + 1 < len(candidates):
                    self.failover_events.append(f"{preset}->{resp.status_code}")
                    nxt = candidates[idx + 1]
                    if nxt in self._candidates:
                        self._activate_candidate(self._candidates.index(nxt))
                    idx += 1
                    continue
                raise last_exc

            if resp.status_code in (401, 403):
                raise ValueError(
                    f"Cloud API rejected request ({resp.status_code}) for 'cloud:{preset}/{api_model}': {resp.text[:200]}"
                )
            if resp.status_code >= 400:
                raise RuntimeError(f"Cloud API error ({resp.status_code}): {resp.text[:240]}")

            parsed_data = resp.json()
            cand_choices = parsed_data.get("choices") or []
            cand_msg = (cand_choices[0].get("message") or {}) if cand_choices else {}
            if not cand_choices or (not (cand_msg.get("content") or "").strip() and not (cand_msg.get("tool_calls") or [])):
                _mark_cooldown(preset, api_model)
                _maybe_inject_gemini_fallback()
                if idx + 1 < len(candidates):
                    self.failover_events.append(f"{preset}->empty")
                    nxt = candidates[idx + 1]
                    if nxt in self._candidates:
                        self._activate_candidate(self._candidates.index(nxt))
                    idx += 1
                    continue

            data = parsed_data
            if (preset, base_url, api_key, api_model) in self._candidates:
                self._activate_candidate(self._candidates.index((preset, base_url, api_key, api_model)))
            else:
                self.preset = preset
                self.base_url = base_url.strip().rstrip("/")
                self.api_key = api_key
                self.api_model = api_model
                self.model = f"cloud:{preset}/{api_model}"
            break

        assert data is not None  # guaranteed if loop exited via break
        choices = data.get("choices") or []
        if not choices:
            raise ValueError(f"Cloud API returned empty choices for '{self.model}'.")

        raw_msg = choices[0].get("message") or {}
        text = raw_msg.get("content") or ""
        parsed_calls: list[Message.ToolCall] = []
        for tc in raw_msg.get("tool_calls") or []:
            func = tc.get("function") or {}
            name = str(func.get("name") or "").strip()
            raw_args = func.get("arguments", {})
            if isinstance(raw_args, str):
                try:
                    args_dict = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    args_dict = {}
            elif isinstance(raw_args, dict):
                args_dict = raw_args
            else:
                args_dict = {}
            if name:
                parsed_calls.append(
                    CloudToolCall(
                        id=(str(tc["id"]) if tc.get("id") else None),
                        extra_content=tc.get("extra_content"),
                        thought_signature=tc.get("thought_signature"),
                        provider_specific_fields=tc.get("provider_specific_fields"),
                        function=Message.ToolCall.Function(name=name, arguments=args_dict),
                    )
                )

        usage = data.get("usage") or {}
        if usage.get("prompt_tokens") is not None:
            self.last_prompt_eval_count = int(usage["prompt_tokens"])
            self.total_prompt_eval_count += self.last_prompt_eval_count
        if usage.get("completion_tokens") is not None:
            self.last_eval_count = int(usage["completion_tokens"])
            self.total_eval_count += self.last_eval_count

        return ChatResponse(
            model=self.model,
            message=Message(
                role="assistant",
                content=text,
                tool_calls=parsed_calls or None,
            ),
        )


class ProviderFactory:
    """Factory for creating local Ollama and optional free cloud LLM providers."""

    @classmethod
    def create_ollama(
        cls,
        model: str | None = None,
        num_ctx: int | None = None,
        temperature: float | None = None,
        num_predict: int | None = None,
        timeout: float | None = None,
    ) -> OllamaProvider:
        """Create Ollama provider with custom options."""
        opts = ModelOptions()
        if num_ctx is not None:
            opts.num_ctx = num_ctx
        if temperature is not None:
            opts.temperature = temperature
        if num_predict is not None:
            opts.num_predict = num_predict
        return OllamaProvider(model=model, default_options=opts, timeout=timeout)

    @classmethod
    def create_cloud(
        cls,
        model: str | None = None,
        num_ctx: int | None = None,
        temperature: float | None = None,
        num_predict: int | None = None,
        timeout: float = 90.0,
    ) -> CloudProvider:
        """Create OpenAI-compatible free cloud LLM provider."""
        opts = ModelOptions()
        if num_ctx is not None:
            opts.num_ctx = num_ctx
        if temperature is not None:
            opts.temperature = temperature
        if num_predict is not None:
            opts.num_predict = num_predict
        return CloudProvider(model=model or "cloud:auto", default_options=opts, timeout=timeout)

    @classmethod
    def create_for_model(
        cls,
        model: str | None = None,
        num_ctx: int | None = None,
        temperature: float | None = None,
        num_predict: int | None = None,
        timeout: float | None = None,
    ) -> OllamaProvider | CloudProvider:
        """Route 'cloud:*' models to CloudProvider and everything else to OllamaProvider."""
        if is_cloud_model_spec(model):
            return cls.create_cloud(
                model=model,
                num_ctx=num_ctx,
                temperature=temperature,
                num_predict=num_predict,
                timeout=timeout or 90.0,
            )
        return cls.create_ollama(
            model=model,
            num_ctx=num_ctx,
            temperature=temperature,
            num_predict=num_predict,
            timeout=timeout,
        )

    @staticmethod
    def available_cloud_models() -> list[str]:
        """List configured free-tier cloud specs ready to select."""
        from ..config.settings import load_aura_env
        load_aura_env()
        found: list[str] = []
        custom_url = _first_env("AURA_CLOUD_BASE_URL", "FREELLMAPI_ENDPOINT", "OPENAI_BASE_URL")
        custom_key = _first_env("AURA_CLOUD_API_KEY", "FREELLMAPI_KEY", "OPENAI_API_KEY")
        if custom_url and custom_key:
            c_model = _first_env("AURA_CLOUD_MODEL", "FREELLMAPI_MODEL") or "auto"
            found.append(f"cloud:custom/{c_model}")
        for name, cfg in CLOUD_PRESETS.items():
            if _first_env(*cfg["key_envs"]):
                found.append(f"cloud:{name}/{_preset_default_model(name)}")
        if len(found) > 1 and "cloud:auto" not in found:
            found.append("cloud:auto")
        return found

    @classmethod
    def create_from_env(cls) -> OllamaProvider | CloudProvider:
        """Create provider using environment variables."""
        env_model = os.environ.get("AGENT_MODEL")
        if os.environ.get("AURA_USE_CLOUD") == "1" or is_cloud_model_spec(env_model):
            return cls.create_cloud(
                model=env_model or "cloud:auto",
                num_ctx=int(os.environ.get("AGENT_NUM_CTX", "12288")),
                temperature=float(os.environ.get("AGENT_TEMPERATURE", "0.0")),
                num_predict=int(os.environ.get("AGENT_SYNTHESIS_MAX_TOKENS", "1536")),
            )
        timeout_env = os.environ.get("AGENT_MODEL_TIMEOUT")
        timeout_val = float(timeout_env) if timeout_env else None
        return cls.create_ollama(
            model=env_model,
            num_ctx=int(os.environ.get("AGENT_NUM_CTX", "12288")),
            temperature=float(os.environ.get("AGENT_TEMPERATURE", "0.0")),
            num_predict=int(os.environ.get("AGENT_SYNTHESIS_MAX_TOKENS", "1536")),
            timeout=timeout_val,
        )


def create_local_qwen_fallback(num_ctx: int | None = None) -> OllamaProvider:
    """Create a fast local Qwen 4B Ollama provider for cloud rate-limit/outage fallback."""
    model = _first_env("AURA_FALLBACK_LOCAL_MODEL", "AURA_LIGHTWEIGHT_MODEL", "AGENT_MODEL") or "qwen3.5:4b"
    if is_cloud_model_spec(model):
        model = "qwen3.5:4b"
    return ProviderFactory.create_ollama(model=model, num_ctx=num_ctx)