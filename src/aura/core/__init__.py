"""
Core module exports for AURA V4.
"""
from .scope import (
    SPECIALIST_AGENTS,
    TaskScope, select_active_tools, detect_git_intent,
    detect_backend_intent, detect_frontend_intent,
    detect_desktop_intent, detect_jarvis_intent, detect_diagnosis_intent, is_conversational,
    normalize_agent_role, parse_agent_prefix,
)
from .fast_router import FastRouter, FastResult
from .personal_memory import PersonalMemoryStore, PersonalFact, extract_explicit_facts, coerce_fact_list, extract_durable_facts
from .voice import VoiceEngine, VoiceConfig, synthesize_wav, apply_megatron_dsp, apply_ultron_dsp
from .memory import SessionMemory, WorkspaceMemoryStore
from .cache import ResultCache
from .mcp_hub import McpHub
from .tools import (
    check_path_policy, sanitize_tool_result, mcp_tool_to_ollama,
    mcp_result_to_text, validate_arguments, normalize_tool_arguments
)
from .evidence import (
    EvidenceItem, evidence_is_sufficient, plan_prefetch_reads,
    extract_source_skeleton, is_prefetch_candidate,
    collect_prefetch_evidence, select_prefetch_policy
)
from .validators import validate_diagnosis_answer, validate_control_flow
from .model_registry import ModelRegistry, ModelCapability, ModelRole
from .model_router import ModelRouter, TaskState, TaskStage, classify_task_role, needs_multi_stage
from .execution_controller import ExecutionController, LoopGuard, LoopGuardConfig, LoopVerdict
from .file_tracker import InspectionTracker, FileInspection
from .orchestrator import run_multi_stage
from .error_classifier import (
    ErrorCategory, ClassifiedResult, classify_tool_result,
    classify_error_message, is_tool_error_text,
)

__all__ = [
    "SPECIALIST_AGENTS",
    "TaskScope",
    "select_active_tools",
    "normalize_agent_role",
    "parse_agent_prefix",
    "detect_git_intent",
    "detect_backend_intent",
    "detect_frontend_intent",
    "detect_desktop_intent",
    "detect_jarvis_intent",
    "detect_diagnosis_intent",
    "is_conversational",
    "FastRouter",
    "FastResult",
    "PersonalMemoryStore",
    "PersonalFact",
    "extract_explicit_facts",
    "coerce_fact_list",
    "extract_durable_facts",
    "VoiceEngine",
    "VoiceConfig",
    "synthesize_wav",
    "apply_megatron_dsp",
    "apply_ultron_dsp",
    "SessionMemory",
    "WorkspaceMemoryStore",
    "ResultCache",
    "McpHub",
    "check_path_policy",
    "sanitize_tool_result",
    "mcp_tool_to_ollama",
    "mcp_result_to_text",
    "validate_arguments",
    "normalize_tool_arguments",
    "EvidenceItem",
    "evidence_is_sufficient",
    "plan_prefetch_reads",
    "extract_source_skeleton",
    "is_prefetch_candidate",
    "collect_prefetch_evidence",
    "select_prefetch_policy",
    "validate_diagnosis_answer",
    "validate_control_flow",
    "ModelRegistry",
    "ModelCapability",
    "ModelRole",
    "ModelRouter",
    "TaskState",
    "TaskStage",
    "classify_task_role",
    "needs_multi_stage",
    "ExecutionController",
    "LoopGuard",
    "LoopGuardConfig",
    "LoopVerdict",
    "InspectionTracker",
    "FileInspection",
    "run_multi_stage",
    "ErrorCategory",
    "ClassifiedResult",
    "classify_tool_result",
    "classify_error_message",
    "is_tool_error_text",
]
