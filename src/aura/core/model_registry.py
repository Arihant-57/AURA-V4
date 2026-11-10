"""
Dynamic Model Registry for AURA V4.

Discovers installed Ollama models, classifies their capabilities,
and provides role-based model selection with fallback logic.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ModelRole(str, Enum):
    """Task roles that map to model selection."""
    LIGHTWEIGHT = "lightweight"  # fast triage, simple questions
    ANALYSIS = "analysis"       # code reading, architecture understanding
    CODING = "coding"           # code generation, editing
    VERIFICATION = "verification"  # review, test analysis


@dataclass(frozen=True)
class ModelCapability:
    """Discovered properties of a single Ollama model."""
    name: str
    parameter_count: float  # billions, e.g. 4.7
    context_length: int
    supports_tools: bool
    supports_vision: bool = False
    supports_thinking: bool = False
    quantization: str = ""
    size_bytes: int = 0

    @property
    def is_small(self) -> bool:
        return self.parameter_count <= 5.0

    @property
    def is_coding_specialist(self) -> bool:
        return "coder" in self.name.lower()


def get_default_role_map() -> dict[ModelRole, list[str]]:
    analysis_env = os.environ.get("AURA_ANALYSIS_MODEL") or os.environ.get("AGENT_ANALYSIS_MODEL")
    coding_env = os.environ.get("AURA_CODING_MODEL") or os.environ.get("AGENT_CODING_MODEL")
    verify_env = os.environ.get("AURA_VERIFICATION_MODEL") or os.environ.get("AGENT_VERIFICATION_MODEL")
    light_env = os.environ.get("AURA_LIGHTWEIGHT_MODEL") or os.environ.get("AGENT_LIGHTWEIGHT_MODEL")

    return {
        ModelRole.LIGHTWEIGHT: [light_env] if light_env else ["qwen3.5:4b", "nemotron-3-nano:4b"],
        ModelRole.ANALYSIS: [analysis_env] if analysis_env else ["qwen3.5:4b", "qwen3.5:9b"],
        ModelRole.CODING: [coding_env] if coding_env else ["qwen2.5-coder:7b", "qwen3.5:4b", "qwen3.5:9b"],
        ModelRole.VERIFICATION: [verify_env] if verify_env else ["qwen3.5:4b", "qwen2.5-coder:7b"],
    }


# Default role → model mapping.
DEFAULT_ROLE_MAP: dict[ModelRole, list[str]] = get_default_role_map()


@dataclass
class ModelRegistry:
    """Discovers and indexes available Ollama models.

    Call ``refresh()`` once at startup (or after pulling models).
    Then use ``select(role)`` to pick the best available model for a task role.
    """

    models: dict[str, ModelCapability] = field(default_factory=dict)
    role_map: dict[ModelRole, list[str]] = field(default_factory=get_default_role_map)
    _refreshed: bool = False

    # ── discovery ───────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Query Ollama for installed models and their capabilities."""
        self.models.clear()
        try:
            import ollama as _ollama
        except ImportError:
            logger.warning("ollama package not installed; registry empty.")
            self._refreshed = True
            return

        try:
            listing = _ollama.list()
        except Exception as exc:
            logger.warning("Cannot list Ollama models: %s", exc)
            self._refreshed = True
            return

        raw_models = listing.get("models", []) if isinstance(listing, dict) else getattr(listing, "models", [])
        for entry in raw_models:
            name = _model_name(entry)
            if not name or _is_embedding_model(name):
                continue
            cap = self._probe_model(name, entry, _ollama)
            if cap is not None:
                self.models[name] = cap

        self._refreshed = True
        logger.info("Model registry refreshed: %d model(s) available.", len(self.models))

    def _probe_model(self, name: str, list_entry: Any, _ollama: Any) -> Optional[ModelCapability]:
        """Get detailed capability info via ``ollama show``."""
        try:
            info = _ollama.show(name)
        except Exception:
            # Fallback: use list_entry metadata only
            info = {}

        details = info.get("details", {}) if isinstance(info, dict) else getattr(info, "details", {}) or {}
        model_info = info.get("model_info", {}) if isinstance(info, dict) else getattr(info, "model_info", {}) or {}
        caps_raw = info.get("capabilities", []) if isinstance(info, dict) else getattr(info, "capabilities", []) or []
        if not isinstance(caps_raw, (list, tuple)):
            caps_raw = []

        param_count = _parse_param_count(details, model_info, name)
        ctx_len = _parse_context_length(details, model_info)
        supports_tools = "tools" in caps_raw
        supports_vision = "vision" in caps_raw
        supports_thinking = "thinking" in caps_raw
        quant = details.get("quantization_level", "") if isinstance(details, dict) else getattr(details, "quantization_level", "")

        size_bytes = 0
        if hasattr(list_entry, "size"):
            size_bytes = list_entry.size or 0
        elif isinstance(list_entry, dict):
            size_bytes = list_entry.get("size", 0) or 0

        return ModelCapability(
            name=name,
            parameter_count=param_count,
            context_length=ctx_len,
            supports_tools=supports_tools,
            supports_vision=supports_vision,
            supports_thinking=supports_thinking,
            quantization=quant or "",
            size_bytes=size_bytes,
        )

    # ── selection ───────────────────────────────────────────────────────

    def select(self, role: ModelRole, *, require_tools: bool = False) -> Optional[str]:
        """Pick the best available model for *role*, with fallback.

        Returns model name or None if nothing suitable is available.
        """
        if not self._refreshed:
            self.refresh()

        candidates = self.role_map.get(role, [])
        for name in candidates:
            cap = self.models.get(name)
            if cap is None:
                continue
            if require_tools and not cap.supports_tools:
                continue
            return name

        # Fallback: any model with tool support if required, else any model
        for cap in sorted(self.models.values(), key=lambda c: c.parameter_count):
            if require_tools and not cap.supports_tools:
                continue
            return cap.name

        return None

    def get(self, name: str) -> Optional[ModelCapability]:
        return self.models.get(name)

    @property
    def available_names(self) -> list[str]:
        return list(self.models.keys())

    def describe(self) -> str:
        if not self.models:
            return "No models available."
        lines = []
        for name, cap in sorted(self.models.items()):
            flags = []
            if cap.supports_tools:
                flags.append("tools")
            if cap.supports_vision:
                flags.append("vision")
            if cap.supports_thinking:
                flags.append("thinking")
            lines.append(
                f"  {name}: {cap.parameter_count:.1f}B params, "
                f"ctx={cap.context_length}, {'+'.join(flags) or 'completion-only'}"
            )
        return "Available models:\n" + "\n".join(lines)


# ── helpers ─────────────────────────────────────────────────────────────

def _model_name(entry: Any) -> str:
    if hasattr(entry, "model"):
        return entry.model or ""
    if isinstance(entry, dict):
        return entry.get("model", "") or entry.get("name", "")
    return ""


def _is_embedding_model(name: str) -> bool:
    lower = name.lower()
    return "embed" in lower or "nomic" in lower


def _parse_param_count(details: Any, model_info: Any, name: str) -> float:
    """Extract parameter count in billions."""
    # Try details.parameter_size (e.g. "4.7B")
    ps = details.get("parameter_size", "") if isinstance(details, dict) else getattr(details, "parameter_size", "")
    if ps:
        try:
            return float(ps.upper().replace("B", "").strip())
        except ValueError:
            pass

    # Try model_info keys containing "param"
    if isinstance(model_info, dict):
        for k, v in model_info.items():
            if "param" in k.lower() and isinstance(v, (int, float)):
                if v > 1_000_000:
                    return v / 1e9
                return float(v)

    # Guess from name (e.g. "qwen3.5:4b" → 4.0)
    import re
    m = re.search(r"(\d+(?:\.\d+)?)b\b", name.lower())
    if m:
        return float(m.group(1))
    return 0.0


def _parse_context_length(details: Any, model_info: Any) -> int:
    # Try model_info keys
    if isinstance(model_info, dict):
        for k, v in model_info.items():
            if "context" in k.lower() and isinstance(v, (int, float)):
                return int(v)
    return 4096  # safe default
