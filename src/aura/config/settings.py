"""
AURA Settings Management

Centralised configuration for the AURA V4 agent.

All tunable values live here so the agent code stays declarative and
testable. Environment variables override the defaults at import time,
which keeps secrets out of source files while remaining configurable
per-run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path


@dataclass
class ModelSettings:
    """Model configuration settings."""
    default_model: str = "qwen3.5:4b"
    complex_model: str = "qwen2.5-coder:7b"
    synthesis_model: str = "qwen3.5:4b"
    num_ctx: int = 12288
    temperature: float = 0.0
    top_p: float = 0.1
    num_predict: int = 1536

    def __post_init__(self):
        self.default_model = os.environ.get("AGENT_MODEL", self.default_model)
        self.complex_model = os.environ.get("AGENT_COMPLEX_MODEL", self.complex_model)
        self.synthesis_model = os.environ.get("AGENT_SYNTHESIS_MODEL", self.synthesis_model)
        self.num_ctx = int(os.environ.get("AGENT_NUM_CTX", str(self.num_ctx)))
        self.temperature = float(os.environ.get("AGENT_TEMPERATURE", str(self.temperature)))
        self.top_p = float(os.environ.get("AGENT_TOP_P", str(self.top_p)))
        self.num_predict = int(os.environ.get("AGENT_SYNTHESIS_MAX_TOKENS", str(self.num_predict)))


@dataclass
class PrefetchSettings:
    """Prefetch / evidence collection settings."""
    enabled: bool = True
    max_reads: int = 8
    manifest_chars: int = 8000
    source_raw_chars: int = 20000
    skeleton_chars: int = 6000
    packet_chars: int = 28000
    execution_order_view_budget: int = 5000
    head_chars: int = 1200
    tail_chars: int = 600

    def __post_init__(self):
        self.enabled = os.environ.get("AGENT_PREFETCH", "1") != "0"
        self.max_reads = int(os.environ.get("AGENT_PREFETCH_MAX_READS", str(self.max_reads)))
        self.manifest_chars = int(os.environ.get("AGENT_PREFETCH_MANIFEST_CHARS", str(self.manifest_chars)))
        self.source_raw_chars = int(os.environ.get("AGENT_PREFETCH_SOURCE_RAW_CHARS", str(self.source_raw_chars)))
        self.skeleton_chars = int(os.environ.get("AGENT_PREFETCH_SKELETON_CHARS", str(self.skeleton_chars)))
        self.packet_chars = int(os.environ.get("AGENT_PREFETCH_PACKET_CHARS", str(self.packet_chars)))
        self.execution_order_view_budget = int(os.environ.get("AGENT_EXECUTION_ORDER_BUDGET", str(self.execution_order_view_budget)))


@dataclass
class MCPSettings:
    """MCP server settings."""
    filesystem_server_filename: str = "filesystem_server.py"
    git_server_filename: str = "git_server.py"
    tool_timeout_seconds: int = 30
    max_tool_result_chars: int = 3000
    shortened_tool_result_chars: int = 600


@dataclass
class CacheSettings:
    """Cache configuration."""
    ttl_seconds: float = 300.0
    max_entries: int = 40

    def __post_init__(self):
        self.ttl_seconds = float(os.environ.get("AGENT_CACHE_TTL", str(self.ttl_seconds)))


@dataclass
class ModelProviderSettings:
    """Model provider configuration."""
    default_provider: str = "ollama"  # ollama, freellmapi
    ollama_model: str = "qwen3.5:4b"
    complex_model: str = "qwen2.5-coder:7b"
    cloud_endpoint: Optional[str] = None
    cloud_api_key: Optional[str] = field(default=None, repr=False)
    cloud_model: str = "claude-3-5-sonnet"

    def __post_init__(self):
        self.ollama_model = os.environ.get("AGENT_MODEL", self.ollama_model)
        self.complex_model = os.environ.get("AGENT_COMPLEX_MODEL", self.complex_model)
        # Keys/endpoints are read from the environment, never hardcoded.
        self.cloud_endpoint = os.environ.get("FREELLMAPI_ENDPOINT", self.cloud_endpoint)
        self.cloud_api_key = os.environ.get("FREELLMAPI_KEY", self.cloud_api_key)


@dataclass
class AgentSettings:
    """Main agent configuration."""
    model: ModelSettings = field(default_factory=ModelSettings)
    prefetch: PrefetchSettings = field(default_factory=PrefetchSettings)
    mcp: MCPSettings = field(default_factory=MCPSettings)
    cache: CacheSettings = field(default_factory=CacheSettings)
    model_provider: ModelProviderSettings = field(default_factory=ModelProviderSettings)
    max_steps: int = 10
    max_tool_result_chars: int = 3000
    context_warn_ratio: float = 0.85
    chars_per_token_estimate: float = 3.2
    keep_recent_tool_results: int = 4
    allow_text_tool_fallback: bool = True
    max_fallback_parse_nudges: int = 2
    force_final_answer: bool = True
    max_empty_reply_nudges: int = 1
    max_duplicate_only_turns: int = 2
    max_diagnosis_nudges: int = 1
    max_change_plan_nudges: int = 1
    mcp_tool_timeout_seconds: int = 30
    model_call_attempts: int = 3
    model_retry_delay_seconds: float = 2.0
    cache_ttl_seconds: float = 300.0
    session_memory_max_entries: int = 40
    debug: bool = False

    def __post_init__(self):
        self.max_steps = int(os.environ.get("AGENT_MAX_STEPS", str(self.max_steps)))
        self.debug = os.environ.get("AGENT_DEBUG") == "1"


class Settings:
    """Global settings singleton."""

    _instance: Optional["Settings"] = None

    def __init__(self):
        self.workspace = None  # WorkspaceConfig is injected at runtime
        self.model = ModelSettings()
        self.prefetch = PrefetchSettings()
        self.mcp = MCPSettings()
        self.cache = CacheSettings()
        self.model_provider = ModelProviderSettings()
        self.agent = AgentSettings()

    @classmethod
    def get_instance(cls) -> "Settings":
        if cls._instance is None:
            cls._instance = Settings()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None


def get_settings() -> Settings:
    """Get global settings instance."""
    return Settings.get_instance()