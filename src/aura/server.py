"""
AURA V4 Localhost Web Server & Browser Workbench.

Wraps the async AURA V4 execution engine in a FastAPI + Uvicorn server
bound strictly to 127.0.0.1, with an asyncio.Lock to enforce sequential
VRAM safety on 6 GB GPUs and SSD-backed chat/project history.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import inspect
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Literal
import uuid

from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agents.aura_v4 import _run_task, format_task_metrics
from .config import resolve_workspace, load_aura_env, save_env_vars, get_default_storage_dir
from .config.workspace import find_referenced_directory, normalize_workspace_path
from .core.telemetry import get_hardware_telemetry
from .core import (
    FastRouter,
    McpHub,
    PersonalMemoryStore,
    ResultCache,
    SessionMemory,
    VoiceEngine,
    WorkspaceMemoryStore,
    extract_durable_facts,
    extract_explicit_facts,
    synthesize_wav,
)
from .core.model_registry import ModelRegistry, ModelRole
from .core.model_router import ModelRouter, needs_multi_stage
from .core.workspace_connector import get_apps_status, get_dashboard_snapshot, save_google_config
from .mcp.desktop_server import todo_note_impl, toggle_todo_item
from .core.providers import (
    CloudProvider,
    ProviderFactory,
    ensure_ollama_running,
    is_cloud_model_spec,
    is_ollama_running,
)
from .core.scope import SPECIALIST_AGENTS, TaskScope, normalize_agent_role, parse_agent_prefix


class WorkspaceRequest(BaseModel):
    path: str


class ModeRequest(BaseModel):
    mode: Literal["ro", "rw"]


class ModelConfigRequest(BaseModel):
    low_vram: bool = False
    model_override: str | None = None


class CloudKeyRequest(BaseModel):
    provider: Literal["groq", "gemini", "openrouter", "github", "custom"]
    api_key: str
    base_url: str | None = None
    model: str | None = None
    use_for_heavy: bool = False


class ChatRequest(BaseModel):
    prompt: str
    agent_role: str | None = None
    push: bool = False
    source: Literal["text", "voice"] = "text"


class SteerRequest(BaseModel):
    prompt: str
    agent_role: str | None = None
    source: Literal["text", "voice"] = "text"
    keep_output: bool = True


class NewChatRequest(BaseModel):
    title: str | None = None
    workspace: str | None = None
    project: str | None = None


class ProjectRequest(BaseModel):
    name: str


class AssignProjectRequest(BaseModel):
    project: str = ""


class VoiceConfigRequest(BaseModel):
    persona: Literal["off", "jarvis", "ultron", "megatron"] = "off"
    auto_speak: bool = False
    prefer_offline: bool = False


class VoiceSpeakRequest(BaseModel):
    text: str
    persona: Literal["jarvis", "ultron", "megatron"] | None = None
    play_local: bool = False


class VoiceListenRequest(BaseModel):
    timeout: float = 12.0


class TodoActionRequest(BaseModel):
    action: Literal["add", "toggle", "clear"]
    text: str = ""
    done: bool = True


class AppConnectRequest(BaseModel):
    gmail_address: str | None = None
    gmail_app_password: str | None = None
    google_oauth_token: str | None = None
    gcal_ical_url: str | None = None
    gmail_enabled: bool | None = None
    calendar_enabled: bool | None = None


class OverlayShowRequest(BaseModel):
    expand: bool = True


class ChatHistoryStore:
    """SSD-backed JSON chat & project history (zero VRAM/RAM overhead)."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or (get_default_storage_dir() / "web_sessions")
        self.root.mkdir(parents=True, exist_ok=True)

    def _projects_file(self) -> Path:
        return self.root / "_projects.json"

    def _load_project_meta(self) -> tuple[list[str], dict[str, float]]:
        pfile = self._projects_file()
        if not pfile.exists():
            return [], {}
        try:
            data = json.loads(pfile.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return [], {}
            raw = data.get("projects", [])
            names = [str(x).strip() for x in raw if str(x).strip()]
            raw_ts = data.get("timestamps", {})
            ts_map = {str(k).strip(): float(v) for k, v in raw_ts.items() if str(k).strip()} if isinstance(raw_ts, dict) else {}
            return names, ts_map
        except Exception:
            return [], {}

    def _save_project_meta(self, projects: list[str], timestamps: dict[str, float]) -> None:
        clean: list[str] = []
        seen: set[str] = set()
        for p in projects:
            name = str(p).strip()
            if name and name not in seen:
                seen.add(name)
                clean.append(name)
        clean_ts = {k: float(timestamps[k]) for k in clean if k in timestamps}
        self._projects_file().write_text(
            json.dumps({"projects": clean, "timestamps": clean_ts}, indent=2),
            encoding="utf-8",
        )

    def _touch_project(self, name: str, ts: float | None = None) -> None:
        clean = " ".join(name.strip().split())
        if not clean:
            return
        projects, ts_map = self._load_project_meta()
        if clean not in projects:
            projects.insert(0, clean)
        ts_map[clean] = max(float(ts_map.get(clean, 0.0)), ts if ts is not None else time.time())
        self._save_project_meta(projects, ts_map)

    def project_timestamps(self) -> dict[str, float]:
        projects, ts_map = self._load_project_meta()
        out: dict[str, float] = {p: float(ts_map.get(p, 0.0)) for p in projects}
        for item in self.list_summaries():
            proj = item.get("project", "").strip()
            if proj:
                out[proj] = max(out.get(proj, 0.0), float(item.get("updated_at", 0.0)))
        return out

    def list_projects(self) -> list[str]:
        ts_map = self.project_timestamps()
        return sorted(ts_map.keys(), key=lambda p: ts_map[p], reverse=True)

    def create_project(self, name: str) -> list[str]:
        clean = " ".join(name.strip().split())
        if not clean:
            raise ValueError("Project name cannot be empty.")
        self._touch_project(clean)
        return self.list_projects()

    def delete_project(self, name: str) -> list[str]:
        target = name.strip()
        projects, ts_map = self._load_project_meta()
        projects = [p for p in projects if p != target]
        ts_map.pop(target, None)
        self._save_project_meta(projects, ts_map)
        for path in self.root.glob("*.json"):
            if path.name.startswith("_"):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("project", "").strip() == target:
                    data["project"] = ""
                    self._save(data)
            except Exception:
                continue
        return self.list_projects()

    def assign_project(self, chat_id: str, project: str) -> dict[str, Any] | None:
        chat = self.get(chat_id)
        if chat is None:
            return None
        clean = " ".join(project.strip().split())
        now = time.time()
        chat["project"] = clean
        chat["updated_at"] = now
        if clean:
            self._touch_project(clean, ts=now)
        self._save(chat)
        return chat

    def _path_for(self, chat_id: str) -> Path:
        safe_id = "".join(ch for ch in chat_id if ch.isalnum() or ch in "-_")
        if not safe_id:
            raise ValueError("Invalid chat_id")
        return self.root / f"{safe_id}.json"

    def create(self, workspace: str, title: str = "New Chat", project: str = "") -> dict[str, Any]:
        now = time.time()
        clean_proj = " ".join(project.strip().split()) if project else ""
        if clean_proj:
            self.create_project(clean_proj)
        chat = {
            "id": uuid.uuid4().hex[:12],
            "title": title,
            "workspace": workspace,
            "project": clean_proj,
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }
        self._save(chat)
        return chat

    def _save(self, chat: dict[str, Any]) -> None:
        path = self._path_for(chat["id"])
        path.write_text(json.dumps(chat, indent=2), encoding="utf-8")

    def get(self, chat_id: str) -> dict[str, Any] | None:
        try:
            path = self._path_for(chat_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("project", "")
            return data
        except Exception:
            return None

    def list_summaries(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in self.root.glob("*.json"):
            if path.name.startswith("_"):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or "id" not in data:
                    continue
                msgs = data.get("messages", [])
                snippet = " ".join(str(m.get("text", ""))[:100] for m in msgs[-6:])[:420]
                items.append(
                    {
                        "id": data["id"],
                        "title": data.get("title", "Untitled"),
                        "workspace": data.get("workspace", ""),
                        "project": data.get("project", ""),
                        "updated_at": data.get("updated_at", 0),
                        "message_count": len(msgs),
                        "search_text": snippet.lower(),
                    }
                )
            except Exception:
                continue
        items.sort(key=lambda x: x["updated_at"], reverse=True)
        return items

    def append_turn(
        self,
        chat_id: str | None,
        workspace: str,
        prompt: str,
        answer: str,
        metrics: str,
        observed_files: list[str] | None = None,
        modified_files: list[str] | None = None,
        cloud: bool = False,
    ) -> dict[str, Any]:
        chat = self.get(chat_id) if chat_id else None
        if chat is None:
            chat = self.create(workspace=workspace, title=self._make_title(prompt))
        elif not chat.get("messages") and chat.get("title") in ("New Chat", ""):
            chat["title"] = self._make_title(prompt)

        now = time.time()
        chat["workspace"] = workspace
        chat["updated_at"] = now
        if chat.get("project"):
            self._touch_project(chat["project"], ts=now)
        chat["messages"].append({"role": "user", "text": prompt, "ts": now})
        chat["messages"].append({"role": "aura", "text": answer, "metrics": metrics, "cloud": bool(cloud), "ts": now})
        if observed_files is not None:
            chat["observed_files"] = observed_files[-40:]
        if modified_files is not None:
            chat["modified_files"] = modified_files[-40:]
        self._save(chat)
        return chat

    def build_memory_context(self, chat_id: str | None, max_turns: int = 3, max_chars: int = 1000) -> str:
        """Build a compact prior-turn summary from SSD so the model remembers previous work."""
        if not chat_id:
            return ""
        chat = self.get(chat_id)
        if not chat:
            return ""
        msgs = chat.get("messages", [])
        if not msgs:
            return ""
        recent = msgs[-(max_turns * 2) :]
        lines: list[str] = []
        for m in recent:
            role = "User" if m.get("role") == "user" else "AURA"
            text = " ".join(str(m.get("text", "")).split())
            if len(text) > 220:
                text = text[:220] + "..."
            lines.append(f"- {role}: {text}")
        summary = "\n".join(lines)
        if len(summary) > max_chars:
            summary = summary[-max_chars:]
        return f"[Project Memory from earlier in this session]\n{summary}\n\n[Current Request]\n"

    def delete(self, chat_id: str) -> bool:
        try:
            path = self._path_for(chat_id)
        except ValueError:
            return False
        if path.exists():
            path.unlink()
            return True
        return False

    @staticmethod
    def _make_title(prompt: str) -> str:
        clean = " ".join(prompt.strip().split())
        return clean[:44] + ("..." if len(clean) > 44 else "")


@dataclass
class AuraSession:
    """Shared runtime state for the AURA V4 web server."""

    hub: McpHub
    registry: ModelRegistry
    router: ModelRouter
    provider: Any
    memory: SessionMemory = field(default_factory=SessionMemory)
    cache: ResultCache = field(default_factory=ResultCache)
    history: ChatHistoryStore = field(default_factory=ChatHistoryStore)
    workspace_memory: WorkspaceMemoryStore | None = None
    personal_memory: PersonalMemoryStore | None = None
    fast_router: FastRouter | None = None
    voice: VoiceEngine | None = None
    workspace_selected: bool = True
    active_chat_id: str | None = None
    authorized_workspaces: set[Path] = field(default_factory=set)
    ctx_override: int | None = None
    model_override: str | None = None
    agent_role: str = "auto"
    low_vram: bool = False
    task_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    busy: bool = False
    current_task: str | None = None
    current_task_handle: asyncio.Task[Any] | None = field(default=None, repr=False)
    overlay_wake_seq: int = 0
    overlay_expand: bool = False
    ensure_overlay_fn: Callable[[], bool] | None = field(default=None, repr=False)
    _hub_queue: asyncio.Queue[Any] | None = field(default=None, repr=False)
    _hub_task: asyncio.Task[Any] | None = field(default=None, repr=False)
    _steer_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _steer_queue: deque[str] = field(default_factory=deque, repr=False)
    steer_seq: int = 0
    steer_history: list[dict[str, Any]] = field(default_factory=list)
    partial_output_chunks: list[str] = field(default_factory=list)
    partial_steps: list[dict[str, Any]] = field(default_factory=list)
    last_checkpoint_text: str = ""
    latest_step: str = ""

    def _build_partial_text_locked(self) -> str:
        if self.partial_output_chunks:
            return "".join(self.partial_output_chunks).strip()
        return (self.last_checkpoint_text or "").strip()

    @property
    def partial_output_text(self) -> str:
        with self._steer_lock:
            return self._build_partial_text_locked()

    def reset_active_run_state(self, *, clear_checkpoint: bool = True) -> None:
        """Clear prior task steer queue and live partial buffers at task start."""
        with self._steer_lock:
            self._steer_queue.clear()
            self.steer_history.clear()
            self.partial_output_chunks.clear()
            self.partial_steps.clear()
            self.latest_step = ""
            if clear_checkpoint:
                self.last_checkpoint_text = ""

    def finish_active_run_state(self, *, preserve_checkpoint: bool = False) -> None:
        """Finalize run buffers on completion or cancellation while keeping/clearing checkpoint cleanly."""
        with self._steer_lock:
            if preserve_checkpoint:
                snapshot = self._build_partial_text_locked()
                if snapshot:
                    self.last_checkpoint_text = snapshot
            else:
                self.last_checkpoint_text = ""
                self.partial_output_chunks.clear()
                self.partial_steps.clear()
                self.latest_step = ""
            self._steer_queue.clear()

    def push_steer(
        self,
        instruction: str,
        *,
        source: str = "text",
        agent_role: str | None = None,
        keep_output: bool = True,
    ) -> dict[str, Any]:
        """Thread-safely enqueue a mid-execution steer instruction while keeping partial findings."""
        clean = " ".join((instruction or "").split()).strip()
        if not clean:
            raise ValueError("Steer instruction cannot be empty.")
        with self._steer_lock:
            self.steer_seq += 1
            seq = self.steer_seq
            if not keep_output:
                self.partial_output_chunks.clear()
                self.last_checkpoint_text = ""
            formatted = f"[agent:{agent_role}] {clean}" if agent_role and agent_role != "auto" else clean
            self._steer_queue.append(formatted)
            entry = {
                "seq": seq,
                "prompt": clean,
                "queued_text": formatted,
                "source": source,
                "agent_role": agent_role or self.agent_role,
                "keep_output": bool(keep_output),
                "timestamp": round(time.time(), 3),
            }
            self.steer_history.append(entry)
            if len(self.steer_history) > 32:
                self.steer_history = self.steer_history[-32:]
            partial_text = self._build_partial_text_locked()
            if partial_text:
                self.last_checkpoint_text = partial_text
            return {
                "seq": seq,
                "entry": entry,
                "pending_steers": list(self._steer_queue),
                "partial_output": partial_text,
                "partial_steps": list(self.partial_steps[-16:]),
                "latest_step": self.latest_step,
            }

    def pop_pending_steers(self) -> list[str]:
        """Atomically drain queued steer instructions in FIFO order for run_agent_loop."""
        with self._steer_lock:
            if not self._steer_queue:
                return []
            items = list(self._steer_queue)
            self._steer_queue.clear()
            return items

    def drain_steers(self) -> list[str]:
        """Alias for pop_pending_steers."""
        return self.pop_pending_steers()

    def peek_pending_steers(self) -> list[str]:
        """Non-destructive snapshot of queued steer instructions."""
        with self._steer_lock:
            return list(self._steer_queue)

    def has_pending_steers(self) -> bool:
        with self._steer_lock:
            return bool(self._steer_queue)

    def snapshot_checkpoint(self) -> dict[str, Any]:
        """Return a thread-safe snapshot of partial findings, steps, and pending steers."""
        with self._steer_lock:
            return {
                "partial_output": self._build_partial_text_locked(),
                "partial_steps": list(self.partial_steps[-16:]),
                "latest_step": self.latest_step,
                "pending_steers": list(self._steer_queue),
                "steer_seq": self.steer_seq,
                "steer_history": list(self.steer_history[-16:]),
            }

    def record_progress_event(self, ev: dict[str, Any] | Any) -> None:
        """Record streaming stage, tool, finding, partial output, and steer events thread-safely."""
        if not isinstance(ev, dict):
            return
        kind = str(ev.get("event") or ev.get("type") or "").strip().lower()
        chunk_text = ""
        if kind in ("partial", "delta", "token", "finding", "answer_chunk") or any(
            k in ev for k in ("delta", "partial", "finding")
        ):
            raw_chunk = (
                ev.get("delta")
                or ev.get("partial")
                or ev.get("finding")
                or ev.get("text")
                or ev.get("content")
                or ev.get("message")
                or ""
            )
            chunk_text = str(raw_chunk)

        summary = str(ev.get("message") or "").strip()
        if not summary:
            if ev.get("tool"):
                summary = f"Tool: {ev.get('tool')}"
            elif ev.get("stage"):
                summary = f"Stage: {ev.get('stage')}"
            elif kind == "steer":
                summary = f"Steered: {ev.get('instruction') or ev.get('prompt') or ''}".strip()
            elif ev.get("finding"):
                summary = str(ev.get("finding")).strip()
            elif kind == "partial" and ev.get("text"):
                summary = str(ev.get("text")).strip()

        with self._steer_lock:
            if chunk_text:
                if kind in ("finding", "partial") and self.partial_output_chunks and not chunk_text.startswith("\n"):
                    last = self.partial_output_chunks[-1]
                    if last and not last.endswith(("\n", " ")):
                        self.partial_output_chunks.append("\n")
                self.partial_output_chunks.append(chunk_text)
                if len(self.partial_output_chunks) > 200:
                    merged = "".join(self.partial_output_chunks)[-24000:]
                    self.partial_output_chunks = [merged]
                snapshot = self._build_partial_text_locked()
                if snapshot:
                    self.last_checkpoint_text = snapshot

            if summary:
                self.latest_step = summary[:200]
                if kind not in ("token", "delta"):
                    self.partial_steps.append(
                        {
                            "event": kind or "progress",
                            "stage": str(ev.get("stage") or ""),
                            "tool": str(ev.get("tool") or ""),
                            "message": summary[:240],
                            "timestamp": round(time.time(), 3),
                        }
                    )
                    if len(self.partial_steps) > 32:
                        self.partial_steps = self.partial_steps[-32:]

    def stop_active(self) -> dict[str, Any]:
        """Cancel any active LLM/tool task and immediately silence local voice output."""
        was_busy = bool(self.busy)
        was_speaking = bool(
            self.voice
            and (self.voice.is_speaking or getattr(self.voice, "is_actively_playing", False))
        )
        checkpoint = self.snapshot_checkpoint()
        with self._steer_lock:
            if checkpoint["partial_output"]:
                self.last_checkpoint_text = checkpoint["partial_output"]
            self._steer_queue.clear()
        cancelled_task = False
        handle = self.current_task_handle
        if handle is not None and not handle.done():
            handle.cancel()
            cancelled_task = True
        if self.voice is not None:
            self.voice.stop()
        return {
            "stopped": True,
            "was_busy": was_busy,
            "was_speaking": was_speaking,
            "cancelled_task": cancelled_task,
            "partial_output": checkpoint["partial_output"],
            "partial_steps": checkpoint["partial_steps"],
            "status": self.status_dict(),
        }

    def __post_init__(self) -> None:
        if self.workspace_memory is None:
            self.workspace_memory = WorkspaceMemoryStore(self.history.root.parent / "workspace_memory")
        if self.personal_memory is None:
            self.personal_memory = PersonalMemoryStore(self.history.root.parent)
        if self.fast_router is None:
            self.fast_router = FastRouter(storage_dir=self.history.root.parent)
        if self.voice is None:
            self.voice = VoiceEngine(storage_dir=self.history.root.parent)

    def start_hub_worker(self) -> None:
        """Run all McpHub lifecycle calls in one task so anyio cancel scopes stay valid."""
        if self._hub_task is not None:
            return
        self._hub_queue = asyncio.Queue()

        async def _worker() -> None:
            assert self._hub_queue is not None
            while True:
                action, payload, fut = await self._hub_queue.get()
                try:
                    if action == "start":
                        await self.hub.start()
                        fut.set_result(None)
                    elif action == "switch":
                        ws_path, ro = payload
                        await self.hub.switch_workspace(ws_path, read_only=ro)
                        fut.set_result(None)
                    elif action == "close":
                        await self.hub.close()
                        fut.set_result(None)
                        break
                except Exception as exc:
                    if not fut.done():
                        fut.set_exception(exc)

        self._hub_task = asyncio.create_task(_worker())

    async def _dispatch_hub(self, action: str, payload: Any = None) -> None:
        loop = asyncio.get_running_loop()
        worker_dead_or_other_loop = (
            self._hub_queue is None
            or self._hub_task is None
            or self._hub_task.done()
            or self._hub_task.get_loop() is not loop
        )
        if worker_dead_or_other_loop:
            if action == "start":
                await self.hub.start()
            elif action == "switch":
                ws_path, ro = payload
                await self.hub.switch_workspace(ws_path, read_only=ro)
            elif action == "close":
                await self.hub.close()
            return
        fut: asyncio.Future[None] = loop.create_future()
        await self._hub_queue.put((action, payload, fut))
        await fut

    async def switch_workspace(self, workspace: Path, read_only: bool = True) -> None:
        await self._dispatch_hub("switch", (workspace, read_only))

    def available_models(self) -> list[str]:
        raw = getattr(self.registry, "models", None)
        if isinstance(raw, dict):
            return sorted(raw.keys())
        return []

    def _serialize_available_tools(self) -> list[dict[str, str]]:
        raw = getattr(self.hub, "tools", None)
        if isinstance(raw, dict):
            return [
                {
                    "name": str(k),
                    "description": (
                        str(getattr(v, "description", "") or "").strip().splitlines()[0][:96]
                        if str(getattr(v, "description", "") or "").strip()
                        else ""
                    ),
                }
                for k, v in sorted(raw.items())
                if k
            ]
        items: list[dict[str, str]] = []
        for t in raw or []:
            name = getattr(t, "name", None)
            if name:
                desc = str(getattr(t, "description", "") or "").strip()
                items.append({
                    "name": str(name),
                    "description": desc.splitlines()[0][:96] if desc else "",
                })
        return items

    def status_dict(self) -> dict[str, Any]:
        ws_facts = 0
        if self.workspace_selected and self.workspace_memory is not None:
            ws_data = self.workspace_memory.load(self.hub.workspace)
            ws_facts = len(ws_data.get("facts", [])) + len(ws_data.get("commands", []))
        prev_override = getattr(self.router, "_override", getattr(self.router, "override", None))
        if prev_override:
            self.router.set_override(None)
        try:
            local_roles = {role.value: self.router.select(role) for role in ModelRole}
        finally:
            if prev_override:
                self.router.set_override(prev_override)
        checkpoint = self.snapshot_checkpoint()
        can_steer = bool(self.busy or self.task_lock.locked())
        return {
            "workspace": str(self.hub.workspace) if self.workspace_selected else "",
            "workspace_name": (self.hub.workspace.name or str(self.hub.workspace)) if self.workspace_selected else "",
            "workspace_selected": self.workspace_selected,
            "read_only": self.hub.read_only,
            "mode": "ro" if self.hub.read_only else "rw",
            "busy": self.busy,
            "can_steer": can_steer,
            "current_task": self.current_task,
            "pending_steers": checkpoint["pending_steers"],
            "steer_seq": checkpoint["steer_seq"],
            "steer_history": checkpoint["steer_history"],
            "partial_output": checkpoint["partial_output"],
            "partial_steps": checkpoint["partial_steps"],
            "latest_step": checkpoint["latest_step"],
            "tools_count": len(self.hub.tools),
            "servers": sorted(self.hub.clients.keys()),
            "available_tools": self._serialize_available_tools(),
            "low_vram": self.low_vram,
            "model_override": self.model_override,
            "agent_role": self.agent_role,
            "available_agents": [
                {"id": k, "label": v["label"], "description": v["description"]}
                for k, v in SPECIALIST_AGENTS.items()
            ],
            "available_models": self.available_models(),
            "ollama_up": bool(self.registry.models) if os.environ.get("PYTEST_CURRENT_TEST") else is_ollama_running(0.35),
            "available_cloud_models": ProviderFactory.available_cloud_models(),
            "cloud_for_heavy": os.environ.get("AURA_CLOUD_FOR_HEAVY") == "1",
            "roles": {role.value: self.router.select(role) for role in ModelRole},
            "local_roles": local_roles,
            "memory_files": len(self.memory.files),
            "memory_dirs": len(self.memory.dirs),
            "workspace_memory_facts": ws_facts,
            "personal_facts": len(self.personal_memory.list_facts()) if self.personal_memory else 0,
            "voice": (
                {
                    "persona": self.voice.get_config().persona,
                    "auto_speak": self.voice.get_config().auto_speak,
                    "prefer_offline": self.voice.get_config().prefer_offline,
                    "speaking": self.voice.is_speaking,
                    "actively_playing": bool(
                        getattr(self.voice, "is_actively_playing", self.voice.is_speaking)
                    ),
                }
                if self.voice
                else {
                    "persona": "off",
                    "auto_speak": False,
                    "prefer_offline": False,
                    "speaking": False,
                    "actively_playing": False,
                }
            ),
            "active_chat_id": self.active_chat_id,
            "history_dir": str(self.history.root),
            "overlay_wake_seq": self.overlay_wake_seq,
            "overlay_expand": self.overlay_expand,
        }

    async def unload_vram(self, all_roles: bool = False) -> list[str]:
        """Evict active local Ollama model(s) from GPU VRAM cleanly."""
        unloaded: list[str] = []
        seen: set[str] = set()
        if hasattr(self.provider, "unload"):
            model_name = str(getattr(self.provider, "model", "") or "")
            try:
                res = self.provider.unload()
                if inspect.isawaitable(res):
                    res = await res
                if res and model_name:
                    unloaded.append(model_name)
                    seen.add(model_name)
            except Exception:
                pass

        if all_roles:
            for role in ModelRole:
                m = self.router.select(role)
                if not m or m in seen or is_cloud_model_spec(m):
                    continue
                seen.add(m)
                try:
                    p = ProviderFactory.create_for_model(model=m, num_ctx=self.ctx_override)
                    if hasattr(p, "unload"):
                        res = p.unload()
                        if inspect.isawaitable(res):
                            res = await res
                        if res:
                            unloaded.append(m)
                except Exception:
                    pass
        return unloaded

    async def close(self) -> None:
        try:
            await self.unload_vram(all_roles=False)
        except Exception:
            pass
        await self._dispatch_hub("close")
        if (
            self._hub_task is not None
            and not self._hub_task.done()
            and self._hub_task.get_loop() is asyncio.get_running_loop()
        ):
            await self._hub_task
        self._hub_task = None


async def _init_session(
    workspace: Path | None = None,
    read_only: bool = True,
    model_override: str | None = None,
    low_vram: bool = False,
    ctx_override: int | None = None,
    history_dir: Path | None = None,
) -> AuraSession:
    load_aura_env()
    history = ChatHistoryStore(history_dir)
    workspace_selected = workspace is not None
    if workspace_selected:
        workspace_path = normalize_workspace_path(str(resolve_workspace(str(workspace))))
        auth_ws = {workspace_path}
    else:
        # Neutral empty directory so launching --web without --workspace never leaks phase4-agent.
        workspace_path = history.root.parent / "empty_workspace"
        workspace_path.mkdir(parents=True, exist_ok=True)
        workspace_path = normalize_workspace_path(str(workspace_path))
        auth_ws = set()
    hub = McpHub(
        workspace_path,
        read_only=read_only,
        enable_desktop=(os.environ.get("AURA_ENABLE_DESKTOP_MCP", "1") == "1"),
        enable_jarvis=(os.environ.get("AURA_ENABLE_JARVIS_MCP", "1") == "1"),
        enable_browser=(os.environ.get("AURA_ENABLE_BROWSER_MCP", "1") == "1"),
    )

    registry = ModelRegistry()
    registry.refresh()
    router = ModelRouter(registry=registry)

    effective_override = "qwen2.5-coder:7b" if low_vram else model_override
    if effective_override:
        router.set_override(effective_override)

    default_model = effective_override or router.select(ModelRole.LIGHTWEIGHT)
    provider = ProviderFactory.create_for_model(model=default_model, num_ctx=ctx_override)

    session = AuraSession(
        hub=hub,
        registry=registry,
        router=router,
        provider=provider,
        history=history,
        workspace_selected=workspace_selected,
        authorized_workspaces=auth_ws,
        ctx_override=ctx_override,
        model_override=model_override,
        low_vram=low_vram,
    )
    session.start_hub_worker()
    await session._dispatch_hub("start")
    return session


def _get_session(app: FastAPI) -> AuraSession:
    session = getattr(app.state, "session", None)
    if session is None:
        raise HTTPException(status_code=503, detail="AURA session is not initialized.")
    return session


def _ensure_not_busy(session: AuraSession) -> None:
    if session.task_lock.locked() or session.busy:
        raise HTTPException(status_code=409, detail="AURA is currently executing a task.")


_STEER_HOOK_MISSING = object()


def _bind_memory_steer_hook(session: AuraSession) -> tuple[Any, Any]:
    """Expose thread-safe steer drainers on session.memory for deep orchestrator stages."""
    mem = session.memory
    if mem is None:
        return (_STEER_HOOK_MISSING, _STEER_HOOK_MISSING)
    mem_vars = getattr(mem, "__dict__", {})
    prev_get = mem_vars.get("get_pending_steer", _STEER_HOOK_MISSING)
    prev_drain = mem_vars.get("drain_steers", _STEER_HOOK_MISSING)
    try:
        setattr(mem, "get_pending_steer", session.pop_pending_steers)
        setattr(mem, "drain_steers", session.pop_pending_steers)
    except Exception:
        pass
    return (prev_get, prev_drain)


def _restore_memory_steer_hook(session: AuraSession, saved: tuple[Any, Any]) -> None:
    mem = session.memory
    if mem is None:
        return
    prev_get, prev_drain = saved
    for attr, prev in (("get_pending_steer", prev_get), ("drain_steers", prev_drain)):
        try:
            if prev is _STEER_HOOK_MISSING:
                if attr in getattr(mem, "__dict__", {}):
                    delattr(mem, attr)
            else:
                setattr(mem, attr, prev)
        except Exception:
            pass


def _build_run_task_kwargs(
    effective_agent: str,
    on_event: Callable[[dict[str, Any]], Any] | None,
    session: AuraSession,
) -> dict[str, Any]:
    """Inspect _run_task signature and pass only supported steering and event hooks."""
    kwargs: dict[str, Any] = {}
    if effective_agent != "auto":
        kwargs["agent_role"] = effective_agent
    try:
        sig = inspect.signature(_run_task)
        params = sig.parameters
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        params = {}
        has_var_kw = True

    if on_event is not None and ("on_event" in params or has_var_kw):
        kwargs["on_event"] = on_event
    if "get_pending_steer" in params or has_var_kw:
        kwargs["get_pending_steer"] = session.pop_pending_steers
    if "steer_queue" in params:
        kwargs["steer_queue"] = None
    return kwargs


async def _maybe_attach_referenced_workspace(session: AuraSession, prompt: str) -> None:
    """Attach a directory explicitly named in *prompt* when no workspace is selected yet."""
    if session.workspace_selected:
        return
    ref_dir = find_referenced_directory(prompt)
    if ref_dir is None:
        return
    new_ws = normalize_workspace_path(str(ref_dir))
    session.authorized_workspaces.add(new_ws)
    await session.switch_workspace(new_ws, read_only=session.hub.read_only)
    session.workspace_selected = True
    session.memory.clear()
    session.cache.clear()


def _prepare_effective_prompt(session: AuraSession, prompt: str, scope: TaskScope) -> str:
    """Combine personal memory, cross-chat workspace memory, and session history cleanly."""
    parts: list[str] = []
    if session.personal_memory is not None and not scope.conversational:
        pmem = session.personal_memory.build_context(prompt)
        if pmem:
            parts.append(pmem)
    if session.workspace_selected and not scope.conversational and session.workspace_memory is not None:
        ws_mem = session.workspace_memory.build_context(session.hub.workspace)
        if ws_mem:
            parts.append(ws_mem)
    chat_mem = session.history.build_memory_context(session.active_chat_id)
    if chat_mem:
        return "\n\n".join(parts + [f"{chat_mem}{prompt}"]) if parts else f"{chat_mem}{prompt}"
    if parts:
        return "\n\n".join(parts) + f"\n\n[Current Request]\n{prompt}"
    return prompt


def _used_cloud_provider(session: AuraSession, active_providers: list[Any]) -> bool:
    if any(isinstance(p, CloudProvider) or is_cloud_model_spec(getattr(p, "model", "")) for p in active_providers):
        return True
    return is_cloud_model_spec(session.model_override)


def _record_turn(
    session: AuraSession,
    prompt: str,
    answer: str,
    metrics: str,
    is_multi: bool,
    cloud: bool = False,
) -> dict[str, Any]:
    """Persist the completed turn to both chat SSD history and cross-chat workspace memory."""
    ws_str = str(session.hub.workspace) if session.workspace_selected else ""
    saved = session.history.append_turn(
        session.active_chat_id,
        ws_str,
        prompt,
        answer,
        metrics,
        observed_files=list(session.memory.files.keys()),
        modified_files=sorted(session.memory.modified),
        cloud=cloud,
    )
    session.active_chat_id = saved["id"]

    if session.personal_memory is not None and not session.hub.read_only:
        for text, cat, trust in extract_explicit_facts(prompt):
            session.personal_memory.add_fact(text, category=cat, trust=trust)
        for cat, text in extract_durable_facts(prompt, answer):
            session.personal_memory.add_fact(text, category=cat, trust="auto")

    if session.workspace_selected and session.workspace_memory is not None and not TaskScope(prompt).conversational:
        key_files = sorted(session.memory.modified) or list(session.memory.files.keys())[-6:]
        commands = WorkspaceMemoryStore.extract_commands(f"{prompt}\n{answer}")
        fact = ""
        if session.memory.modified or is_multi:
            first_line = next((ln.strip() for ln in answer.splitlines() if ln.strip() and not ln.strip().startswith("#")), "")
            fact = f"{ChatHistoryStore._make_title(prompt)}: {first_line[:110]}" if first_line else ChatHistoryStore._make_title(prompt)
        if key_files or commands or fact:
            session.workspace_memory.record_turn(
                session.hub.workspace,
                key_files=key_files,
                commands=commands,
                fact=fact,
            )
    return saved


def create_app(
    workspace: Path | None = None,
    read_only: bool = True,
    model_override: str | None = None,
    low_vram: bool = False,
    ctx_override: int | None = None,
    history_dir: Path | None = None,
    shutdown_fn: Callable[[], None] | None = None,
    ensure_overlay_fn: Callable[[], bool] | None = None,
) -> FastAPI:
    """Build the FastAPI application for AURA V4."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owns_session = getattr(app.state, "session", None) is None
        if owns_session:
            app.state.session = await _init_session(
                workspace=workspace,
                read_only=read_only,
                model_override=model_override,
                low_vram=low_vram,
                ctx_override=ctx_override,
                history_dir=history_dir,
            )
        if ensure_overlay_fn is not None and getattr(app.state, "session", None) is not None:
            app.state.session.ensure_overlay_fn = ensure_overlay_fn
        try:
            yield
        finally:
            if owns_session and getattr(app.state, "session", None) is not None:
                try:
                    await asyncio.wait_for(app.state.session.close(), timeout=4.0)
                except Exception:
                    pass

    app = FastAPI(title="AURA V4", lifespan=lifespan)
    app.state.shutdown_fn = shutdown_fn
    _static_dir = Path(__file__).resolve().parent / "static"
    if _static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _INDEX_HTML

    @app.get("/island", response_class=HTMLResponse)
    async def island() -> str:
        return _ISLAND_HTML

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        return _get_session(app).status_dict()

    @app.get("/api/telemetry")
    async def get_telemetry() -> dict[str, Any]:
        return get_hardware_telemetry()

    @app.get("/api/chats")
    async def list_chats() -> dict[str, Any]:
        session = _get_session(app)
        return {
            "chats": session.history.list_summaries(),
            "projects": session.history.list_projects(),
            "project_timestamps": session.history.project_timestamps(),
            "active_chat_id": session.active_chat_id,
        }

    @app.post("/api/projects")
    async def create_project(req: ProjectRequest) -> dict[str, Any]:
        session = _get_session(app)
        try:
            projects = session.history.create_project(req.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"projects": projects}

    @app.delete("/api/projects/{project_name}")
    async def delete_project(project_name: str) -> dict[str, Any]:
        session = _get_session(app)
        projects = session.history.delete_project(project_name)
        return {"projects": projects, "chats": session.history.list_summaries()}

    @app.post("/api/chats")
    async def create_chat(req: NewChatRequest | None = None) -> dict[str, Any]:
        session = _get_session(app)
        _ensure_not_busy(session)
        if req and req.workspace:
            candidate = Path(req.workspace.strip()).expanduser()
            if not candidate.exists() or not candidate.is_dir():
                raise HTTPException(status_code=400, detail=f"Directory not found: {req.workspace}")
            new_ws = normalize_workspace_path(str(candidate))
            session.authorized_workspaces.add(new_ws)
            await session.switch_workspace(new_ws, read_only=True)
            session.workspace_selected = True
            session.memory.clear()
            session.cache.clear()
        chat = session.history.create(
            workspace=str(session.hub.workspace) if session.workspace_selected else "",
            title=(req.title.strip() if req and req.title else "New Chat") or "New Chat",
            project=(req.project.strip() if req and req.project else ""),
        )
        session.active_chat_id = chat["id"]
        return {"chat": chat, "status": session.status_dict()}

    @app.post("/api/chats/{chat_id}/project")
    async def assign_chat_project(chat_id: str, req: AssignProjectRequest) -> dict[str, Any]:
        session = _get_session(app)
        chat = session.history.assign_project(chat_id, req.project)
        if chat is None:
            raise HTTPException(status_code=404, detail="Chat not found.")
        return {"chat": chat, "projects": session.history.list_projects()}

    @app.get("/api/chats/{chat_id}")
    async def get_chat(chat_id: str) -> dict[str, Any]:
        session = _get_session(app)
        chat = session.history.get(chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="Chat not found.")
        session.active_chat_id = chat["id"]
        ws_str = chat.get("workspace")
        if ws_str:
            candidate = Path(ws_str)
            if candidate.exists() and candidate.is_dir():
                target = normalize_workspace_path(str(candidate))
                session.workspace_selected = True
                if target != session.hub.workspace:
                    session.authorized_workspaces.add(target)
                    await session.switch_workspace(target, read_only=True)
                    session.memory.clear()
                    session.cache.clear()
        for fpath in chat.get("observed_files", []):
            session.memory.note_file(fpath)
        for mpath in chat.get("modified_files", []):
            session.memory.note_modified(mpath)
        return {"chat": chat, "status": session.status_dict()}

    @app.delete("/api/chats/{chat_id}")
    async def delete_chat(chat_id: str) -> dict[str, Any]:
        session = _get_session(app)
        deleted = session.history.delete(chat_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Chat not found.")
        if session.active_chat_id == chat_id:
            session.active_chat_id = None
        return {"deleted": chat_id, "active_chat_id": session.active_chat_id}

    @app.post("/api/workspace")
    async def switch_workspace(req: WorkspaceRequest) -> dict[str, Any]:
        session = _get_session(app)
        _ensure_not_busy(session)
        candidate = Path(req.path.strip()).expanduser()
        if not candidate.exists() or not candidate.is_dir():
            raise HTTPException(status_code=400, detail=f"Directory not found: {req.path}")

        new_workspace = normalize_workspace_path(str(candidate))
        session.authorized_workspaces.add(new_workspace)
        await session.switch_workspace(new_workspace, read_only=True)
        session.workspace_selected = True
        session.memory.clear()
        session.cache.clear()
        return session.status_dict()

    @app.post("/api/mode")
    async def switch_mode(req: ModeRequest) -> dict[str, Any]:
        session = _get_session(app)
        read_only = req.mode == "ro"
        if session.hub.read_only == read_only:
            return session.status_dict()
        # Keep toggling instant during idle or active turns while updating hub read_only flag
        session.hub.read_only = read_only
        if not session.task_lock.locked() and not session.busy:
            await session.switch_workspace(session.hub.workspace, read_only=read_only)
        return session.status_dict()

    @app.post("/api/model")
    async def configure_model(req: ModelConfigRequest) -> dict[str, Any]:
        session = _get_session(app)
        _ensure_not_busy(session)
        session.low_vram = req.low_vram
        session.model_override = req.model_override.strip() if req.model_override else None
        effective = "qwen2.5-coder:7b" if session.low_vram else session.model_override
        if effective and is_cloud_model_spec(effective):
            # Evict pinned local Ollama weights from 6 GB VRAM when switching to cloud API.
            session.router.set_override(None)
            await session.unload_vram(all_roles=True)
        session.router.set_override(effective)

        default_model = effective or session.router.select(ModelRole.LIGHTWEIGHT)
        if getattr(session.provider, "model", None) != default_model and hasattr(session.provider, "unload"):
            session.provider.unload()
        session.provider = ProviderFactory.create_for_model(model=default_model, num_ctx=session.ctx_override)
        return session.status_dict()

    @app.post("/api/cloud-key")
    async def save_cloud_key(req: CloudKeyRequest) -> dict[str, Any]:
        session = _get_session(app)
        _ensure_not_busy(session)
        key_val = req.api_key.strip()
        if not key_val:
            raise HTTPException(status_code=400, detail="API key cannot be empty.")

        env_map = {
            "groq": "GROQ_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "github": "GITHUB_TOKEN",
            "custom": "AURA_CLOUD_API_KEY",
        }
        model_env_map = {
            "groq": "GROQ_MODEL",
            "gemini": "GEMINI_MODEL",
            "openrouter": "OPENROUTER_MODEL",
            "github": "GITHUB_MODEL",
            "custom": "AURA_CLOUD_MODEL",
        }
        updates: dict[str, str] = {
            env_map[req.provider]: key_val,
            "AURA_CLOUD_FOR_HEAVY": "1" if req.use_for_heavy else "0",
        }
        if req.model and req.model.strip():
            updates[model_env_map[req.provider]] = req.model.strip()
        if req.provider == "custom":
            if not (req.base_url and req.base_url.strip()):
                raise HTTPException(status_code=400, detail="Base URL is required for custom OpenAI-compatible API.")
            updates["AURA_CLOUD_BASE_URL"] = req.base_url.strip()

        saved_path = save_env_vars(updates, env_path=session.history.root.parent / ".env")
        return {
            "saved_to": str(saved_path),
            "provider": req.provider,
            "status": session.status_dict(),
        }

    @app.post("/api/reset")
    async def reset_session() -> dict[str, Any]:
        session = _get_session(app)
        _ensure_not_busy(session)
        session.memory.clear()
        session.cache.clear()
        return session.status_dict()

    @app.post("/api/vram/unload")
    async def unload_vram_endpoint() -> dict[str, Any]:
        session = _get_session(app)
        _ensure_not_busy(session)
        unloaded = await session.unload_vram(all_roles=True)
        return {"unloaded": unloaded, "status": session.status_dict()}

    @app.post("/api/ollama/start")
    async def start_ollama_endpoint() -> dict[str, Any]:
        session = _get_session(app)
        started = await asyncio.to_thread(ensure_ollama_running, 10.0)
        if started:
            await asyncio.to_thread(session.registry.refresh)
            current_model = str(getattr(session.provider, "model", "") or "")
            if not is_cloud_model_spec(current_model):
                sel = session.model_override or session.router.select(ModelRole.LIGHTWEIGHT)
                if sel:
                    session.provider = ProviderFactory.create_for_model(model=sel, num_ctx=session.ctx_override)
        return {"started": started, "status": session.status_dict()}

    @app.post("/api/shutdown")
    async def shutdown_endpoint() -> dict[str, Any]:
        session = getattr(app.state, "session", None)
        unloaded: list[str] = []
        if session is not None:
            try:
                session.stop_active()
            except Exception:
                pass
            try:
                unloaded = await asyncio.wait_for(session.unload_vram(all_roles=True), timeout=4.0)
            except Exception:
                pass
        fn = getattr(app.state, "shutdown_fn", None)
        if callable(fn):
            asyncio.get_running_loop().call_later(0.15, fn)
        return {"shutting_down": True, "unloaded": unloaded}

    @app.get("/api/voice/status")
    async def voice_status() -> dict[str, Any]:
        session = _get_session(app)
        cfg = session.voice.get_config() if session.voice else None
        return {
            "persona": cfg.persona if cfg else "off",
            "auto_speak": cfg.auto_speak if cfg else False,
            "prefer_offline": cfg.prefer_offline if cfg else False,
            "personas": ["off", "jarvis", "ultron", "megatron"],
        }

    @app.post("/api/voice/config")
    async def voice_config(req: VoiceConfigRequest) -> dict[str, Any]:
        session = _get_session(app)
        if session.voice is None:
            session.voice = VoiceEngine(storage_dir=session.history.root.parent)
        cfg = session.voice.update_config(
            persona=req.persona,
            auto_speak=req.auto_speak,
            prefer_offline=req.prefer_offline,
        )
        if cfg.persona == "off":
            session.voice.stop()
        return {"persona": cfg.persona, "auto_speak": cfg.auto_speak, "prefer_offline": cfg.prefer_offline, "status": session.status_dict()}

    @app.post("/api/voice/speak")
    async def voice_speak(req: VoiceSpeakRequest) -> Response:
        session = _get_session(app)
        cfg = session.voice.get_config() if session.voice else None
        target = req.persona or (cfg.persona if cfg and cfg.persona in ("jarvis", "ultron", "megatron") else "jarvis")
        if req.play_local and session.voice is not None:
            session.voice.speak_async(req.text, persona=target)  # type: ignore[arg-type]
            return Response(content=json.dumps({"speaking": True, "persona": target}), media_type="application/json")
        try:
            wav_path = await asyncio.to_thread(
                synthesize_wav,
                req.text,
                target,  # type: ignore[arg-type]
                None,
                bool(cfg.prefer_offline if cfg else False),
            )
            data = wav_path.read_bytes()
            try:
                wav_path.unlink(missing_ok=True)
            except OSError:
                pass
            return Response(content=data, media_type="audio/wav")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Voice synthesis failed: {exc}") from exc

    @app.post("/api/voice/listen")
    async def voice_listen(req: VoiceListenRequest | None = None) -> dict[str, Any]:
        if os.environ.get("AURA_HEADLESS_TEST") == "1":
            return {"transcript": "", "ok": False}

        import threading
        from .core.mic_listener import MicListener

        timeout_s = max(2.0, min(float(req.timeout if req else 12.0), 25.0))

        def _capture_one() -> tuple[str, str]:
            done_evt = threading.Event()
            heard: list[str] = []
            errs: list[str] = []

            def _on_tx(txt: str) -> None:
                clean = (txt or "").strip()
                if clean and not heard:
                    heard.append(clean)
                    done_evt.set()

            def _on_err(msg: str) -> None:
                if msg and not errs:
                    errs.append(msg)
                done_evt.set()

            listener = MicListener(on_transcript=_on_tx, on_error=_on_err)
            try:
                if not listener.start():
                    return ("", errs[0] if errs else "Microphone unavailable.")
                done_evt.wait(timeout=timeout_s)
                return (heard[0] if heard else "", errs[0] if errs else "")
            finally:
                listener.stop()

        transcript, err = await asyncio.to_thread(_capture_one)
        out: dict[str, Any] = {"transcript": transcript, "ok": bool(transcript)}
        if err and not transcript:
            out["error"] = err
        return out

    @app.post("/api/stop")
    async def stop_endpoint() -> dict[str, Any]:
        session = _get_session(app)
        return session.stop_active()

    @app.post("/api/voice/stop")
    async def voice_stop_endpoint() -> dict[str, Any]:
        session = _get_session(app)
        was_speaking = bool(session.voice and session.voice.is_speaking)
        if session.voice is not None:
            session.voice.stop()
        return {"stopped": True, "was_speaking": was_speaking, "status": session.status_dict()}

    @app.post("/api/overlay/show")
    async def show_overlay_endpoint(req: OverlayShowRequest | None = None) -> dict[str, Any]:
        session = _get_session(app)
        session.overlay_wake_seq += 1
        session.overlay_expand = True if req is None else bool(req.expand)
        spawned = False
        if session.ensure_overlay_fn is not None:
            try:
                spawned = bool(session.ensure_overlay_fn())
            except Exception:
                spawned = False
        try:
            from .overlay import signal_overlay_wake_event

            signal_overlay_wake_event()
        except Exception:
            pass
        return {
            "ok": True,
            "spawned": spawned,
            "overlay_wake_seq": session.overlay_wake_seq,
            "expand": session.overlay_expand,
        }

    @app.get("/api/dashboard-data")
    async def dashboard_data_endpoint() -> dict[str, Any]:
        session = _get_session(app)
        return get_dashboard_snapshot(session.history.root.parent)

    @app.post("/api/todos")
    async def todos_action_endpoint(req: TodoActionRequest) -> dict[str, Any]:
        session = _get_session(app)
        base = session.history.root.parent
        clean = req.text.strip()
        if req.action == "add":
            if not clean:
                raise HTTPException(status_code=400, detail="To-Do item text cannot be empty.")
            todo_note_impl("add", clean, storage_dir=base, read_only=False)
        elif req.action == "toggle":
            if not clean:
                raise HTTPException(status_code=400, detail="To-Do item text cannot be empty.")
            toggle_todo_item(clean, bool(req.done), storage_dir=base)
        elif req.action == "clear":
            todo_note_impl("clear", clean, storage_dir=base, read_only=False)
        return get_dashboard_snapshot(base)

    @app.get("/api/apps/status")
    async def apps_status_endpoint() -> dict[str, Any]:
        session = _get_session(app)
        return {"apps": get_apps_status(session.history.root.parent)}

    @app.post("/api/apps/connect")
    async def apps_connect_endpoint(req: AppConnectRequest) -> dict[str, Any]:
        session = _get_session(app)
        base = session.history.root.parent
        try:
            apps = save_google_config(req.model_dump(exclude_none=True), storage_dir=base)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"apps": apps, "dashboard": get_dashboard_snapshot(base)}

    @app.post("/api/chat")
    async def chat(req: ChatRequest) -> dict[str, Any]:
        session = _get_session(app)
        prompt = req.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="Prompt cannot be empty.")
        if session.task_lock.locked() or session.busy:
            if not req.push:
                _ensure_not_busy(session)
            pushed = session.push_steer(
                prompt,
                source=req.source,
                agent_role=req.agent_role,
                keep_output=True,
            )
            return {
                "pushed": True,
                "steered": True,
                "queued": True,
                "keep_output": True,
                "prompt": prompt,
                "source": req.source,
                "agent_role": req.agent_role or session.agent_role,
                "steer_seq": pushed["seq"],
                "pending_steers": pushed["pending_steers"],
                "partial_output": pushed["partial_output"],
                "partial_steps": pushed["partial_steps"],
                "latest_step": pushed["latest_step"],
                "status": session.status_dict(),
            }

        async with session.task_lock:
            session.busy = True
            session.current_task = prompt
            session.reset_active_run_state(clear_checkpoint=True)
            saved_mem_hooks = _bind_memory_steer_hook(session)
            was_cancelled = False
            try:
                await _maybe_attach_referenced_workspace(session, prompt)
                prefixed_agent, _ = parse_agent_prefix(prompt)
                effective_agent = normalize_agent_role(req.agent_role or prefixed_agent or session.agent_role)
                fast = (
                    session.fast_router.try_handle(prompt, read_only=session.hub.read_only)
                    if session.fast_router and effective_agent in ("auto", "jarvis")
                    else None
                )
                if fast and fast.handled:
                    metrics = f"Tier-0 FastRouter ({fast.action}, {fast.latency_ms} ms, 0 LLM tokens)"
                    saved = _record_turn(session, prompt, fast.answer, metrics, False, cloud=False)
                    return {
                        "answer": fast.answer,
                        "metrics": metrics,
                        "multi_stage": False,
                        "cloud": False,
                        "agent_role": effective_agent,
                        "chat_id": saved["id"],
                        "status": session.status_dict(),
                    }
                scope = TaskScope(prompt, agent_role=effective_agent)
                is_multi = (effective_agent == "auto") and needs_multi_stage(prompt, scope)
                effective_prompt = _prepare_effective_prompt(session, prompt, scope)

                async def _emit_http_event(ev: dict[str, Any]) -> None:
                    session.record_progress_event(ev)

                run_kwargs = _build_run_task_kwargs(effective_agent, _emit_http_event, session)
                task = asyncio.create_task(
                    _run_task(
                        effective_prompt,
                        session.router,
                        session.provider,
                        session.hub,
                        session.memory,
                        session.cache,
                        session.ctx_override,
                        **run_kwargs,
                    )
                )
                session.current_task_handle = task
                answer, active_providers = await task
                metrics = format_task_metrics(active_providers)
                cloud_used = _used_cloud_provider(session, active_providers)
                saved = _record_turn(session, prompt, answer, metrics, is_multi, cloud=cloud_used)
                session.finish_active_run_state(preserve_checkpoint=False)
                return {
                    "answer": answer,
                    "metrics": metrics,
                    "multi_stage": is_multi,
                    "cloud": cloud_used,
                    "agent_role": effective_agent,
                    "chat_id": saved["id"],
                    "status": session.status_dict(),
                }
            except asyncio.CancelledError:
                was_cancelled = True
                checkpoint = session.snapshot_checkpoint()
                session.finish_active_run_state(preserve_checkpoint=True)
                partial_ans = checkpoint["partial_output"]
                return {
                    "answer": partial_ans or "Stopped by user.",
                    "partial_output": partial_ans,
                    "partial_steps": checkpoint["partial_steps"],
                    "metrics": "Stopped by user",
                    "multi_stage": False,
                    "cloud": False,
                    "stopped": True,
                    "status": session.status_dict(),
                }
            finally:
                _restore_memory_steer_hook(session, saved_mem_hooks)
                if not was_cancelled:
                    session.finish_active_run_state(preserve_checkpoint=False)
                session.current_task_handle = None
                session.busy = False
                session.current_task = None

    @app.post("/api/chat/steer")
    async def steer_chat(req: SteerRequest) -> dict[str, Any]:
        session = _get_session(app)
        prompt = req.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="Steer instruction cannot be empty.")
        if not (session.task_lock.locked() or session.busy):
            checkpoint = session.snapshot_checkpoint()
            return {
                "steered": False,
                "queued": False,
                "reason": "no_active_task",
                "keep_output": bool(req.keep_output),
                "prompt": prompt,
                "source": req.source,
                "agent_role": req.agent_role or session.agent_role,
                "steer_seq": checkpoint["steer_seq"],
                "pending_steers": checkpoint["pending_steers"],
                "partial_output": checkpoint["partial_output"],
                "partial_steps": checkpoint["partial_steps"],
                "latest_step": checkpoint["latest_step"],
                "status": session.status_dict(),
            }
        pushed = session.push_steer(
            prompt,
            source=req.source,
            agent_role=req.agent_role,
            keep_output=bool(req.keep_output),
        )
        return {
            "steered": True,
            "queued": True,
            "keep_output": bool(req.keep_output),
            "prompt": prompt,
            "source": req.source,
            "agent_role": req.agent_role or session.agent_role,
            "steer_seq": pushed["seq"],
            "pending_steers": pushed["pending_steers"],
            "partial_output": pushed["partial_output"],
            "partial_steps": pushed["partial_steps"],
            "latest_step": pushed["latest_step"],
            "status": session.status_dict(),
        }

    @app.websocket("/ws/chat")
    async def ws_chat(websocket: WebSocket) -> None:
        await websocket.accept()
        session = _get_session(app)
        try:
            while True:
                payload = await websocket.receive_json()
                prompt = str(payload.get("prompt", "")).strip()
                if not prompt:
                    await websocket.send_json({"type": "error", "message": "Prompt cannot be empty."})
                    continue
                if session.task_lock.locked() or session.busy:
                    await websocket.send_json(
                        {"type": "error", "message": "AURA is currently executing a task. Please wait."}
                    )
                    continue

                async with session.task_lock:
                    session.busy = True
                    session.current_task = prompt
                    session.reset_active_run_state(clear_checkpoint=True)
                    saved_mem_hooks = _bind_memory_steer_hook(session)
                    was_cancelled = False
                    try:
                        await _maybe_attach_referenced_workspace(session, prompt)
                        raw_agent = payload.get("agent_role")
                        prefixed_agent, _ = parse_agent_prefix(prompt)
                        effective_agent = normalize_agent_role(raw_agent or prefixed_agent or session.agent_role)
                        fast = (
                            session.fast_router.try_handle(prompt, read_only=session.hub.read_only)
                            if session.fast_router and effective_agent in ("auto", "jarvis")
                            else None
                        )
                        if fast and fast.handled:
                            metrics = f"Tier-0 FastRouter ({fast.action}, {fast.latency_ms} ms, 0 LLM tokens)"
                            saved = _record_turn(session, prompt, fast.answer, metrics, False, cloud=False)
                            await websocket.send_json(
                                {
                                    "type": "result",
                                    "answer": fast.answer,
                                    "metrics": metrics,
                                    "multi_stage": False,
                                    "cloud": False,
                                    "agent_role": effective_agent,
                                    "chat_id": saved["id"],
                                    "status": session.status_dict(),
                                }
                            )
                            continue
                        scope = TaskScope(prompt, agent_role=effective_agent)
                        is_multi = (effective_agent == "auto") and needs_multi_stage(prompt, scope)
                        mode_label = (
                            "Multi-stage (Sequential)"
                            if is_multi
                            else (f"Agent: {effective_agent}" if effective_agent != "auto" else "Single-pass")
                        )
                        await websocket.send_json(
                            {
                                "type": "status",
                                "multi_stage": is_multi,
                                "agent_role": effective_agent,
                                "message": f"Running {mode_label} pipeline...",
                            }
                        )

                        async def _emit_ws(ev: dict[str, Any]) -> None:
                            session.record_progress_event(ev)
                            try:
                                await websocket.send_json({"type": "progress", **ev})
                            except Exception:
                                pass

                        effective_prompt = _prepare_effective_prompt(session, prompt, scope)
                        run_kwargs = _build_run_task_kwargs(effective_agent, _emit_ws, session)
                        task = asyncio.create_task(
                            _run_task(
                                effective_prompt,
                                session.router,
                                session.provider,
                                session.hub,
                                session.memory,
                                session.cache,
                                session.ctx_override,
                                **run_kwargs,
                            )
                        )
                        session.current_task_handle = task
                        answer, active_providers = await task
                        metrics = format_task_metrics(active_providers)
                        cloud_used = _used_cloud_provider(session, active_providers)
                        saved = _record_turn(session, prompt, answer, metrics, is_multi, cloud=cloud_used)
                        session.finish_active_run_state(preserve_checkpoint=False)
                        await websocket.send_json(
                            {
                                "type": "result",
                                "answer": answer,
                                "metrics": metrics,
                                "multi_stage": is_multi,
                                "cloud": cloud_used,
                                "agent_role": effective_agent,
                                "chat_id": saved["id"],
                                "status": session.status_dict(),
                            }
                        )
                    except asyncio.CancelledError:
                        was_cancelled = True
                        checkpoint = session.snapshot_checkpoint()
                        session.finish_active_run_state(preserve_checkpoint=True)
                        await websocket.send_json(
                            {
                                "type": "stopped",
                                "message": "Stopped by user.",
                                "partial_output": checkpoint["partial_output"],
                                "partial_steps": checkpoint["partial_steps"],
                                "status": session.status_dict(),
                            }
                        )
                    except Exception as exc:
                        await websocket.send_json({"type": "error", "message": str(exc)})
                    finally:
                        _restore_memory_steer_hook(session, saved_mem_hooks)
                        if not was_cancelled:
                            session.finish_active_run_state(preserve_checkpoint=False)
                        session.current_task_handle = None
                        session.busy = False
                        session.current_task = None
        except WebSocketDisconnect:
            return

    return app


_VENDOR_HEAD_SCRIPTS = """
  <script defer src="/static/vendor/gsap.min.js"></script>
  <script defer src="/static/vendor/Flip.min.js"></script>
  <script defer src="/static/vendor/floating-ui.core.umd.min.js"></script>
  <script defer src="/static/vendor/floating-ui.dom.umd.min.js"></script>
  <script defer src="/static/vendor/siriwave.umd.min.js"></script>
  <script defer src="/static/vendor/lucide.min.js"></script>
  <script defer src="/static/vendor/motion.min.js"></script>
  <script type="module">
    import autoAnimate from "/static/vendor/auto-animate.min.js";
    window.autoAnimate = autoAnimate;
  </script>
  <script>
    window.addEventListener("DOMContentLoaded", () => {
      if (window.gsap && window.Flip) {
        window.gsap.registerPlugin(window.Flip);
      }
    });
  </script>"""

_INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>AURA V4</title>""" + _VENDOR_HEAD_SCRIPTS + r"""
  <style>
    :root {
      --bg: #0d0d0d;
      --sidebar: #171717;
      --surface: #212121;
      --composer: #2f2f2f;
      --border: #2e2e2e;
      --text: #ececec;
      --muted: #9b9b9b;
      --accent: #38bdf8;
      --ok: #22c55e;
      --warn: #f59e0b;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      height: 100vh;
      display: flex;
      overflow: hidden;
    }
    aside {
      width: 272px;
      background: var(--sidebar);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      flex-shrink: 0;
      transition: width 0.2s ease, margin-left 0.2s ease;
    }
    aside.collapsed { width: 0; overflow: hidden; border-right: none; }
    .side-top {
      padding: 12px;
      display: flex;
      gap: 8px;
      align-items: center;
    }
    .new-chat-btn {
      flex: 1;
      display: flex;
      align-items: center;
      gap: 8px;
      background: transparent;
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 9px 12px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
    }
    .new-chat-btn:hover { background: var(--surface); }
    .new-proj-btn {
      background: transparent;
      color: var(--muted);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 9px 10px;
      font-size: 12px;
      font-weight: 500;
      cursor: pointer;
      white-space: nowrap;
    }
    .new-proj-btn:hover { background: var(--surface); color: var(--text); }
    .side-search {
      padding: 0 12px 6px;
    }
    .side-search input {
      width: 100%;
      background: #111111;
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 6px 10px;
      font-size: 12px;
      outline: none;
    }
    .side-search input:focus { border-color: #4b5563; }
    .side-label {
      padding: 8px 14px 4px;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.6px;
      color: var(--muted);
    }
    .section-tag {
      padding: 8px 8px 3px;
      font-size: 10.5px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--muted);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .proj-group {
      display: flex;
      flex-direction: column;
      gap: 2px;
      margin-bottom: 4px;
    }
    .proj-head {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 6px 8px;
      border-radius: 8px;
      font-size: 12.5px;
      font-weight: 600;
      color: var(--text);
      cursor: pointer;
      user-select: none;
    }
    .proj-head:hover { background: var(--surface); }
    .proj-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .proj-count { font-size: 11px; color: var(--muted); font-weight: 400; }
    .proj-mini-btn {
      opacity: 0;
      background: transparent;
      border: none;
      color: var(--muted);
      cursor: pointer;
      padding: 1px 5px;
      border-radius: 4px;
      font-size: 12.5px;
    }
    .proj-head:hover .proj-mini-btn { opacity: 1; }
    .proj-mini-btn:hover { color: var(--text); background: #2f2f2f; }
    .proj-chats {
      display: flex;
      flex-direction: column;
      gap: 2px;
      padding-left: 12px;
      border-left: 1px solid #262626;
      margin-left: 12px;
    }
    .proj-group.collapsed .proj-chats { display: none; }
    .proj-empty { padding: 4px 10px; font-size: 11.5px; color: var(--muted); font-style: italic; }
    #chatList {
      flex: 1;
      overflow-y: auto;
      padding: 4px 8px;
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .chat-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 4px;
      padding: 7px 8px;
      border-radius: 8px;
      cursor: pointer;
      font-size: 13px;
      color: var(--text);
    }
    .chat-item:hover { background: var(--surface); }
    .chat-item.active { background: #2a2a2a; }
    .chat-meta { overflow: hidden; flex: 1; }
    .chat-title {
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      font-weight: 500;
    }
    .chat-sub {
      font-size: 11px;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .more-btn {
      opacity: 0.72;
      background: transparent;
      border: none;
      color: var(--muted);
      cursor: pointer;
      padding: 2px 6px;
      border-radius: 6px;
      font-size: 16px;
      line-height: 1;
      letter-spacing: 0.5px;
      flex-shrink: 0;
    }
    .chat-item:hover .more-btn, .chat-item.active .more-btn, .more-btn:focus { opacity: 1; }
    .more-btn:hover { color: var(--text); background: #2f2f2f; }
    .chat-menu {
      position: fixed;
      z-index: 80;
      min-width: 184px;
      background: #1d1d1d;
      border: 1px solid #383838;
      border-radius: 10px;
      padding: 6px;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.55);
      display: none;
      flex-direction: column;
      gap: 2px;
    }
    .chat-menu.open { display: flex; }
    .chat-menu-label {
      padding: 4px 8px;
      font-size: 10.5px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.4px;
      color: var(--muted);
    }
    .chat-menu-item {
      width: 100%;
      text-align: left;
      background: transparent;
      border: none;
      color: var(--text);
      font-size: 12.5px;
      padding: 6px 8px;
      border-radius: 6px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    .chat-menu-item:hover { background: #2c2c2c; }
    .chat-menu-item.danger { color: #f87171; }
    .chat-menu-item.danger:hover { background: rgba(239, 68, 68, 0.14); }
    .chat-menu-sep { height: 1px; background: var(--border); margin: 4px 0; }

    .side-footer {
      border-top: 1px solid var(--border);
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      font-size: 12px;
    }
    .ws-box {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 8px 10px;
    }
    .ws-row { display: flex; justify-content: space-between; align-items: center; gap: 6px; }
    .ws-path {
      font-size: 11px;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      margin-top: 4px;
    }
    .pill-group { display: flex; gap: 6px; }
    .pill-btn {
      flex: 1;
      background: var(--surface);
      color: var(--muted);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 6px 8px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
    }
    .pill-btn:hover { color: var(--text); }
    .pill-btn.active-ro { background: #0c4a6e; color: #e0f2fe; border-color: var(--accent); }
    .pill-btn.active-rw { background: #78350f; color: #fef3c7; border-color: var(--warn); }
    .link-btn {
      background: transparent;
      border: none;
      color: var(--accent);
      font-size: 11px;
      cursor: pointer;
      padding: 0;
    }

    .main-wrap {
      flex: 1;
      display: flex;
      flex-direction: column;
      min-width: 0;
      height: 100vh;
    }
    header {
      height: 52px;
      padding: 0 16px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 1px solid #1b1b1b;
    }
    .header-left { display: flex; align-items: center; gap: 10px; }
    .icon-btn {
      background: transparent;
      border: 1px solid transparent;
      color: var(--muted);
      border-radius: 8px;
      padding: 6px 9px;
      cursor: pointer;
      font-size: 15px;
    }
    .icon-btn:hover { background: var(--surface); color: var(--text); }
    select.model-select {
      background: transparent;
      color: var(--text);
      border: 1px solid transparent;
      border-radius: 8px;
      padding: 6px 10px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      outline: none;
    }
    select.model-select:hover { background: var(--surface); }
    select.model-select option { background: var(--sidebar); color: var(--text); }
    .header-right { display: flex; align-items: center; gap: 8px; }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 12px;
      background: var(--sidebar);
      border: 1px solid var(--border);
      color: var(--muted);
    }
    .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ok); }
    .dot.busy { background: var(--warn); }

    #stage {
      flex: 1;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 20px 16px;
    }
    #emptyHero {
      margin: auto;
      text-align: center;
      max-width: 680px;
      width: 100%;
    }
    #emptyHero h1 {
      font-size: 28px;
      font-weight: 600;
      margin: 0 0 8px;
      letter-spacing: -0.3px;
    }
    #emptyHero p {
      color: var(--muted);
      font-size: 13px;
      margin: 0 0 22px;
    }
    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: center;
      margin-top: 16px;
    }
    .chip {
      background: var(--sidebar);
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 999px;
      padding: 7px 14px;
      font-size: 12px;
      cursor: pointer;
    }
    .chip:hover { background: var(--surface); }

    #messages {
      width: 100%;
      max-width: 760px;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }
    .msg { line-height: 1.6; font-size: 14.5px; word-break: break-word; }
    .msg.user {
      align-self: flex-end;
      background: var(--composer);
      padding: 10px 16px;
      border-radius: 18px;
      max-width: 78%;
      white-space: pre-wrap;
    }
    .msg.aura {
      align-self: flex-start;
      width: 100%;
      padding: 4px 2px;
      white-space: pre-wrap;
    }
    .msg.meta {
      align-self: center;
      font-size: 12px;
      color: var(--muted);
    }
    .msg pre {
      background: var(--sidebar);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px;
      overflow-x: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12.5px;
      margin: 10px 0;
    }
    .msg code {
      background: var(--surface);
      padding: 2px 5px;
      border-radius: 5px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12.5px;
    }
    .msg pre code { background: transparent; padding: 0; }
    .msg table {
      width: 100%;
      border-collapse: collapse;
      margin: 10px 0;
      font-size: 13px;
    }
    .msg th, .msg td {
      border: 1px solid var(--border);
      padding: 7px 10px;
      text-align: left;
    }
    .msg th { background: var(--sidebar); font-weight: 600; }
    .msg ul, .msg ol { margin: 6px 0; padding-left: 22px; }
    .msg h2, .msg h3, .msg h4 { margin: 12px 0 6px; font-weight: 600; }
    .metrics {
      font-size: 11px;
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }
    .aura-footer {
      margin-top: 8px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      min-height: 24px;
    }
    .retry-btn {
      width: 26px;
      height: 26px;
      border-radius: 50%;
      border: 1px solid #3f3f3f;
      background: #232323;
      color: var(--muted);
      font-size: 13px;
      line-height: 1;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease;
    }
    .retry-btn:hover:not(:disabled) {
      background: #2f2f2f;
      color: var(--text);
      border-color: var(--accent);
    }
    .retry-btn:disabled { opacity: 0.35; cursor: not-allowed; }
    @keyframes auraPulse {
      0%, 100% { opacity: 0.35; transform: scale(0.9); }
      50% { opacity: 1; transform: scale(1.1); }
    }
    .activity-card {
      align-self: flex-start;
      width: 100%;
      background: #141414;
      border: 1px solid #292929;
      border-radius: 12px;
      padding: 10px 14px;
      font-size: 12.5px;
    }
    .activity-head {
      display: flex;
      align-items: center;
      gap: 8px;
      font-weight: 600;
      color: var(--text);
    }
    .pulse-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--accent);
      animation: auraPulse 1.3s ease-in-out infinite;
      flex-shrink: 0;
    }
    .activity-steps {
      margin-top: 8px;
      padding-left: 16px;
      border-left: 1px solid #2b2b2b;
      display: flex;
      flex-direction: column;
      gap: 4px;
      color: var(--muted);
      font-size: 12px;
    }
    .activity-step {
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .activity-step.latest { color: var(--text); }
    .activity-done {
      align-self: flex-start;
      margin-bottom: -10px;
      font-size: 12px;
      color: var(--muted);
    }
    .activity-done summary {
      cursor: pointer;
      list-style: none;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 3px 10px;
      border-radius: 999px;
      background: #161616;
      border: 1px solid #292929;
      user-select: none;
    }
    .activity-done summary:hover { color: var(--text); border-color: #3f3f3f; }
    .activity-done .activity-steps { margin: 6px 0 8px 6px; }

    .composer-dock {
      width: 100%;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 8px 16px 16px;
    }
    form#composer {
      width: 100%;
      max-width: 780px;
      background: var(--composer);
      border: 1px solid #3e3e3e;
      border-radius: 22px;
      padding: 10px 12px 8px 16px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      box-shadow: 0 6px 24px rgba(0, 0, 0, 0.35);
    }
    form#composer textarea {
      width: 100%;
      background: transparent;
      border: none;
      color: var(--text);
      font-family: inherit;
      font-size: 14.5px;
      line-height: 1.45;
      resize: none;
      outline: none;
      height: 26px;
      max-height: 160px;
      min-height: 26px;
      overflow-y: hidden;
      padding: 2px 0;
    }
    .composer-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      flex-wrap: nowrap;
    }
    .composer-controls {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: nowrap;
      min-width: 0;
      overflow-x: auto;
      scrollbar-width: none;
    }
    .composer-controls::-webkit-scrollbar { display: none; }
    .bar-select { max-width: 220px; text-overflow: ellipsis; }
    .bar-select, .bar-pill {
      background: #232323;
      color: var(--text);
      border: 1px solid #3d3d3d;
      border-radius: 999px;
      padding: 5px 10px;
      font-size: 12px;
      font-weight: 500;
      cursor: pointer;
      outline: none;
    }
    .bar-select:hover, .bar-pill:hover { background: #2c2c2c; border-color: #525252; }
    .bar-select option { background: var(--sidebar); color: var(--text); }
    .seg-toggle {
      display: inline-flex;
      background: #1a1a1a;
      border: 1px solid #3d3d3d;
      border-radius: 999px;
      padding: 2px;
    }
    .seg-btn {
      background: transparent;
      color: var(--muted);
      border: none;
      border-radius: 999px;
      padding: 3px 10px;
      font-size: 11.5px;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s ease, color 0.15s ease;
    }
    .seg-btn.active-ro { background: #0c4a6e; color: #e0f2fe; }
    .seg-btn.active-rw { background: #92400e; color: #fef3c7; }
    .composer-actions {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-shrink: 0;
    }
    .voice-action-btn {
      height: 32px;
      padding: 0 11px;
      font-size: 12px;
      font-weight: 500;
      display: inline-flex;
      align-items: center;
      gap: 5px;
      white-space: nowrap;
    }
    .voice-action-btn.active-dictate {
      background: #0c4a6e;
      color: #e0f2fe;
      border-color: #38bdf8;
      box-shadow: 0 0 10px rgba(56, 189, 248, 0.35);
    }
    .voice-action-btn.active-talk {
      background: #14532d;
      color: #dcfce7;
      border-color: #4ade80;
      box-shadow: 0 0 10px rgba(74, 222, 128, 0.35);
    }
    #sendBtn {
      position: relative;
      width: 34px;
      height: 34px;
      border-radius: 50%;
      border: none;
      background: #ffffff;
      color: #0d0d0d;
      font-size: 16px;
      font-weight: 700;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      transition: background 0.15s ease, color 0.15s ease;
    }
    #sendBtn:disabled { opacity: 0.35; cursor: not-allowed; }
    #sendBtn.stopping {
      background: #1f2937;
      color: #ffffff;
    }
    #sendBtn.stopping::after {
      content: "";
      position: absolute;
      inset: -3px;
      border-radius: 50%;
      border: 2px solid rgba(56, 189, 248, 0.22);
      border-top-color: #38bdf8;
      animation: spinRing 0.85s linear infinite;
      pointer-events: none;
    }
    .stop-square {
      width: 11px;
      height: 11px;
      border-radius: 2px;
      background: #ffffff;
      display: inline-block;
    }
    @keyframes spinRing {
      to { transform: rotate(360deg); }
    }
    .footer-hint {
      margin-top: 6px;
      font-size: 11px;
      color: var(--muted);
    }
    .code-wrap { position: relative; margin: 10px 0; }
    .copy-code-btn {
      position: absolute;
      top: 8px;
      right: 8px;
      background: #2a2a2a;
      color: var(--muted);
      border: 1px solid #3f3f3f;
      border-radius: 6px;
      padding: 2px 8px;
      font-size: 11px;
      cursor: pointer;
    }
    .copy-code-btn:hover { color: var(--text); background: #383838; }
    .modal-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.65);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 50;
    }
    .modal-backdrop.open { display: flex; }
    .modal-card {
      width: 92%;
      max-width: 460px;
      background: var(--sidebar);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 18px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .modal-card h3 { margin: 0; font-size: 15px; }
    .modal-card input {
      width: 100%;
      background: var(--bg);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 9px 12px;
      font-size: 13px;
      outline: none;
    }
    .modal-actions { display: flex; justify-content: flex-end; gap: 8px; }
  </style>
</head>
<body>
  <aside id="sidebar">
    <div class="side-top">
      <button id="newChatBtn" class="new-chat-btn">+ New chat</button>
      <button id="newProjectBtn" class="new-proj-btn" title="Create SSD project">+ Project</button>
    </div>
    <div class="side-search">
      <input id="chatSearchInput" type="search" placeholder="Search projects or chats..." aria-label="Search projects or chats" />
    </div>
    <div class="side-label">Projects &amp; Chats (SSD)</div>
    <div id="chatList"></div>
    <div class="side-footer">
      <div class="ws-row">
        <span id="toolsInfo" style="color:var(--muted)">0 tools</span>
        <div style="display:flex; gap:10px; align-items:center;">
          <button id="unloadVramBtn" class="link-btn" title="Unload local model(s) from GPU VRAM">Free VRAM</button>
          <button id="resetBtn" class="link-btn" title="Clear temporary RAM file/tool cache (SSD chats are kept)">Clear RAM</button>
          <button id="shutdownBtn" class="link-btn" style="color:#fca5a5;" title="Unload VRAM and shut down AURA V4">Shut Down</button>
        </div>
      </div>
    </div>
  </aside>

  <div class="main-wrap">
    <header>
      <div class="header-left">
        <button id="toggleSidebarBtn" class="icon-btn" title="Toggle sidebar">&#9776;</button>
        <span style="font-weight:600; font-size:14px;">AURA V4</span>
      </div>
      <div class="header-right">
        <button type="button" id="appsCornerBtn" class="bar-pill" title="Connect Gmail &amp; Google Calendar">&#8862; Apps</button>
        <span class="status-pill"><span id="statusDot" class="dot"></span><span id="statusText">Ready</span></span>
      </div>
    </header>

    <div id="stage">
      <div id="emptyHero">
        <h1>Hey, ready to build?</h1>
        <p id="heroSub">Sequential local agent · Per-project memory saved on SSD</p>
        <div class="chips">
          <button class="chip" data-prompt="Summarize this workspace structure and main entry points.">Inspect workspace</button>
          <button class="chip" data-prompt="Check the project for syntax or runtime issues.">Check for bugs</button>
          <button class="chip" data-prompt="List available tools and active models.">Tools &amp; models</button>
        </div>
      </div>
      <div id="messages"></div>
    </div>

    <div class="composer-dock">
      <form id="composer">
        <textarea id="promptInput" rows="1" placeholder="Ask AURA V4 anything about your workspace..."></textarea>
        <div class="composer-bar">
          <div class="composer-controls">
            <select id="agentSelect" class="bar-select" title="Select specialist sub-agent or Auto sequential orchestrator">
              <option value="auto">Agent: Auto (Orchestrator)</option>
              <option value="researcher">Agent: Researcher</option>
              <option value="planner">Agent: Planner</option>
              <option value="coder">Agent: Coder</option>
              <option value="tester">Agent: Tester</option>
              <option value="debugger">Agent: Debugger</option>
              <option value="reviewer">Agent: Reviewer</option>
              <option value="jarvis">Agent: Jarvis Desktop</option>
            </select>
            <select id="modelSelect" class="bar-select" title="Select pipeline or downloaded Ollama model">
              <option value="auto">AURA Sequential (4B + 7B)</option>
              <option value="low_vram">AURA Low-VRAM (7B)</option>
            </select>
            <select id="voiceSelect" class="bar-select" title="Free Jarvis / Ultron / Megatron Voice TTS (Zero subscription)">
              <option value="off">Voice: Off</option>
              <option value="jarvis">Voice: Jarvis (Free)</option>
              <option value="ultron">Voice: Ultron DSP</option>
              <option value="megatron">Voice: Megatron DSP</option>
            </select>
            <div class="seg-toggle" title="Toggle Read-Only (RO) or Read-Write (RW)">
              <button type="button" id="roBtn" class="seg-btn active-ro">RO</button>
              <button type="button" id="rwBtn" class="seg-btn">RW</button>
            </div>
            <button type="button" id="changeWsBtn" class="bar-pill" title="Switch project workspace">
              &#128193; <span id="wsName">Workspace</span>
            </button>
          </div>
          <div class="composer-actions">
            <button id="dictateBtn" type="button" class="bar-pill voice-action-btn" title="Speak to type into the search bar (Speech-to-Text)">&#127908; Dictate</button>
            <button id="liveTalkBtn" type="button" class="bar-pill voice-action-btn" title="Direct two-way voice conversation with Jarvis / Ultron">&#9673; Jarvis Live</button>
            <button id="sendBtn" type="submit" title="Send">&#8593;</button>
          </div>
        </div>
      </form>
      <div class="footer-hint" id="wsPathHint">127.0.0.1 · Sequential VRAM lock</div>
    </div>
  </div>

  <div id="wsModal" class="modal-backdrop">
    <div class="modal-card">
      <h3>Switch Project Workspace</h3>
      <input id="wsModalInput" type="text" placeholder="C:\\path\\to\\project" />
      <div class="modal-actions">
        <button type="button" id="wsModalCancel" class="bar-pill">Cancel</button>
        <button type="button" id="wsModalSave" class="bar-pill" style="background:#fff; color:#0d0d0d;">Switch</button>
      </div>
    </div>
  </div>

  <div id="rwModal" class="modal-backdrop">
    <div class="modal-card">
      <h3>Enable Read-Write (RW) Mode?</h3>
      <p style="margin:0; font-size:13px; color:var(--muted); line-height:1.5;">
        AURA will be allowed to create, edit, and modify files inside the active workspace.
      </p>
      <div class="modal-actions">
        <button type="button" id="rwModalCancel" class="bar-pill">Stay Read-Only</button>
        <button type="button" id="rwModalConfirm" class="bar-pill" style="background:#f59e0b; color:#0d0d0d; border-color:#f59e0b;">Enable RW</button>
      </div>
    </div>
  </div>

  <div id="projectModal" class="modal-backdrop">
    <div class="modal-card">
      <h3>New Project (SSD)</h3>
      <input id="projectModalInput" type="text" placeholder="Project name (e.g. AURA Backend, Billing API)" />
      <div class="modal-actions">
        <button type="button" id="projectModalCancel" class="bar-pill">Cancel</button>
        <button type="button" id="projectModalSave" class="bar-pill" style="background:#fff; color:#0d0d0d;">Create</button>
      </div>
    </div>
  </div>

  <div id="cloudModal" class="modal-backdrop">
    <div class="modal-card">
      <h3>Free Cloud LLM API (Saved to ~/.aura/.env)</h3>
      <select id="cloudProviderSelect" style="background:#262626; color:var(--text); border:1px solid var(--border); border-radius:8px; padding:8px 10px; font-size:13px;">
        <option value="groq">Groq (GROQ_API_KEY)</option>
        <option value="gemini">Google Gemini (GEMINI_API_KEY)</option>
        <option value="openrouter">OpenRouter Free (OPENROUTER_API_KEY)</option>
        <option value="github">GitHub Models (GITHUB_TOKEN)</option>
        <option value="custom">Custom OpenAI-Compatible Endpoint</option>
      </select>
      <input id="cloudKeyInput" type="password" placeholder="Paste API key once (persists across reboots, 0 idle RAM)" />
      <input id="cloudBaseUrlInput" type="text" placeholder="Base URL (custom only, e.g. http://127.0.0.1:31415/v1)" style="display:none;" />
      <input id="cloudModelInput" type="text" placeholder="Model ID (optional, e.g. gemini-3.8-flash)" />
      <label style="display:flex; align-items:center; gap:8px; font-size:12px; color:var(--muted); cursor:pointer;">
        <input id="cloudHeavyToggle" type="checkbox" />
        Auto-use Cloud for heavy multi-file tasks (auto-fallback to local if offline)
      </label>
      <div class="modal-actions">
        <button type="button" id="cloudModalCancel" class="bar-pill">Cancel</button>
        <button type="button" id="cloudModalSave" class="bar-pill" style="background:#fff; color:#0d0d0d;">Save Key</button>
      </div>
    </div>
  </div>

  <div id="deleteChatModal" class="modal-backdrop">
    <div class="modal-card">
      <h3>Delete Chat from SSD?</h3>
      <p id="deleteChatText" style="margin:0; font-size:13px; color:var(--muted); line-height:1.5;">
        This permanently removes the chat file from SSD.
      </p>
      <div class="modal-actions">
        <button type="button" id="deleteChatCancel" class="bar-pill">No</button>
        <button type="button" id="deleteChatConfirm" class="bar-pill" style="background:#ef4444; color:#fff; border-color:#ef4444;">Yes, Delete</button>
      </div>
    </div>
  </div>

  <div id="appsModal" class="modal-backdrop">
    <div class="modal-card" style="width:420px; max-width:92vw;">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <h3 style="margin:0;">Connect Workspace Apps</h3>
        <span id="appsSaveState" style="font-size:11px; color:var(--muted);"></span>
      </div>
      <div style="display:flex; flex-direction:column; gap:8px; padding:10px 12px; border:1px solid var(--border); border-radius:10px; background:#141414;">
        <label style="display:flex; justify-content:space-between; align-items:center; font-size:13px; font-weight:600; cursor:pointer;">
          <span>Gmail <span id="appsGmailBadge" style="font-weight:400; font-size:11px; color:var(--muted); margin-left:6px;">Disconnected</span></span>
          <input id="appsGmailToggle" type="checkbox" />
        </label>
        <input id="appsGmailAddr" type="email" placeholder="Gmail address (you@gmail.com)" />
        <input id="appsGmailPass" type="password" placeholder="Gmail App Password (leaves existing intact if blank)" />
      </div>
      <div style="display:flex; flex-direction:column; gap:8px; padding:10px 12px; border:1px solid var(--border); border-radius:10px; background:#141414;">
        <label style="display:flex; justify-content:space-between; align-items:center; font-size:13px; font-weight:600; cursor:pointer;">
          <span>Google Calendar <span id="appsCalBadge" style="font-weight:400; font-size:11px; color:var(--muted); margin-left:6px;">Local</span></span>
          <input id="appsCalToggle" type="checkbox" />
        </label>
        <input id="appsCalIcalUrl" type="url" placeholder="Private iCal feed (.ics URL)" />
      </div>
      <div class="modal-actions">
        <button type="button" id="appsModalClose" class="bar-pill">Close</button>
        <button type="button" id="appsModalSave" class="bar-pill" style="background:#fff; color:#0d0d0d;">Save &amp; Connect</button>
      </div>
    </div>
  </div>

  <div id="chatActionMenu" class="chat-menu"></div>

  <script>
    const sidebar = document.getElementById("sidebar");
    const chatListEl = document.getElementById("chatList");
    const messagesEl = document.getElementById("messages");
    const emptyHero = document.getElementById("emptyHero");
    const promptEl = document.getElementById("promptInput");
    const sendBtn = document.getElementById("sendBtn");
    const agentSelect = document.getElementById("agentSelect");
    const modelSelect = document.getElementById("modelSelect");
    const voiceSelect = document.getElementById("voiceSelect");
    let currentVoiceAudio = null;
    let isBusyState = false;
    let isVoicePlaying = false;
    function syncActionBtn() {
      const stopping = isBusyState || isVoicePlaying;
      sendBtn.classList.toggle("stopping", stopping);
      sendBtn.type = stopping ? "button" : "submit";
      sendBtn.disabled = false;
      if (stopping) {
        sendBtn.innerHTML = '<span class="stop-square" aria-label="Stop"></span>';
        sendBtn.title = "Stop execution";
      } else {
        sendBtn.innerHTML = "&#8593;";
        sendBtn.title = "Send";
      }
    }
    async function stopExecution() {
      if (currentVoiceAudio) {
        try { currentVoiceAudio.pause(); } catch {}
        currentVoiceAudio = null;
      }
      isVoicePlaying = false;
      try { await postJson("/api/stop", {}); } catch {}
      clearLiveProgress(false);
      setBusy(false, "Stopped");
      syncActionBtn();
    }
    sendBtn.addEventListener("click", (e) => {
      if (isBusyState || isVoicePlaying) {
        e.preventDefault();
        stopExecution();
      }
    });
    async function speakAnswerInBrowser(text) {
      const persona = voiceSelect ? voiceSelect.value : "off";
      if (!persona || persona === "off" || !text) return;
      try {
        if (currentVoiceAudio) { currentVoiceAudio.pause(); currentVoiceAudio = null; }
        isVoicePlaying = true;
        syncActionBtn();
        const res = await fetch("/api/voice/speak", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text, persona }),
        });
        if (!res.ok) {
          isVoicePlaying = false;
          syncActionBtn();
          return;
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        currentVoiceAudio = audio;
        const done = () => {
          URL.revokeObjectURL(url);
          if (currentVoiceAudio === audio) currentVoiceAudio = null;
          isVoicePlaying = false;
          syncActionBtn();
        };
        audio.onended = done;
        audio.onerror = done;
        await audio.play().catch(done);
      } catch {
        isVoicePlaying = false;
        syncActionBtn();
      }
    }
    const statusDot = document.getElementById("statusDot");
    const statusText = document.getElementById("statusText");
    const wsNameEl = document.getElementById("wsName");
    const wsPathHintEl = document.getElementById("wsPathHint");
    const toolsInfoEl = document.getElementById("toolsInfo");
    const roBtn = document.getElementById("roBtn");
    const rwBtn = document.getElementById("rwBtn");
    const wsModal = document.getElementById("wsModal");
    const wsModalInput = document.getElementById("wsModalInput");
    const rwModal = document.getElementById("rwModal");
    const projectModal = document.getElementById("projectModal");
    const projectModalInput = document.getElementById("projectModalInput");
    const cloudModal = document.getElementById("cloudModal");
    const cloudProviderSelect = document.getElementById("cloudProviderSelect");
    const cloudKeyInput = document.getElementById("cloudKeyInput");
    const cloudBaseUrlInput = document.getElementById("cloudBaseUrlInput");
    const cloudModelInput = document.getElementById("cloudModelInput");
    const cloudHeavyToggle = document.getElementById("cloudHeavyToggle");

    let ws = null;
    let activeChatId = null;
    let currentWorkspace = "";
    let lastUserPrompt = "";
    let liveActivity = null;
    const collapsedProjects = new Set();

    function scrollStageNearBottom() {
      const stage = document.getElementById("stage");
      const gap = stage.scrollHeight - stage.scrollTop - stage.clientHeight;
      if (gap < 140) stage.scrollTop = stage.scrollHeight;
    }

    function updateLiveProgress(text) {
      const clean = (text || "").trim();
      if (!clean) return;
      if (!liveActivity) {
        const card = document.createElement("div");
        card.className = "activity-card";
        card.innerHTML = `<div class="activity-head"><span class="pulse-dot"></span><span class="activity-title">Working...</span></div><div class="activity-steps"></div>`;
        messagesEl.appendChild(card);
        syncEmptyState();
        liveActivity = {
          card,
          titleEl: card.querySelector(".activity-title"),
          stepsEl: card.querySelector(".activity-steps"),
          steps: [],
        };
      }
      liveActivity.titleEl.textContent = clean.length > 64 ? "Working..." : clean;
      if (liveActivity.steps[liveActivity.steps.length - 1] !== clean) {
        liveActivity.steps.push(clean);
        const visible = liveActivity.steps.slice(-6);
        liveActivity.stepsEl.innerHTML = "";
        visible.forEach((s, idx) => {
          const row = document.createElement("div");
          row.className = "activity-step" + (idx === visible.length - 1 ? " latest" : "");
          row.textContent = s;
          liveActivity.stepsEl.appendChild(row);
        });
        scrollStageNearBottom();
      }
    }

    function clearLiveProgress(keepSummary = false) {
      if (!liveActivity) return;
      if (keepSummary && liveActivity.steps.length > 0) {
        const details = document.createElement("details");
        details.className = "activity-done";
        const summary = document.createElement("summary");
        summary.textContent = `Worked · ${liveActivity.steps.length} step${liveActivity.steps.length === 1 ? "" : "s"}`;
        const list = document.createElement("div");
        list.className = "activity-steps";
        liveActivity.steps.forEach((s) => {
          const row = document.createElement("div");
          row.className = "activity-step";
          row.textContent = s;
          list.appendChild(row);
        });
        details.appendChild(summary);
        details.appendChild(list);
        liveActivity.card.replaceWith(details);
      } else {
        liveActivity.card.remove();
      }
      liveActivity = null;
      syncEmptyState();
    }

    function escapeHtml(str) {
      return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    function renderInline(str) {
      return str
        .replace(/`([^`\n]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    }

    function renderMarkdown(text) {
      const lines = escapeHtml(text).split("\n");
      const out = [];
      let i = 0;
      while (i < lines.length) {
        const line = lines[i];
        const fenceMatch = line.match(/^```([\w-]*)\s*$/);
        if (fenceMatch) {
          const lang = fenceMatch[1] || "code";
          const codeLines = [];
          i++;
          while (i < lines.length && !/^```\s*$/.test(lines[i])) {
            codeLines.push(lines[i]);
            i++;
          }
          out.push(
            `<div class="code-wrap"><button type="button" class="copy-code-btn">Copy</button><pre><code data-lang="${lang}">${codeLines.join("\n")}</code></pre></div>`
          );
          i++;
          continue;
        }
        if (line.startsWith("|") && i + 1 < lines.length && /^\|[\s:-]+\|/.test(lines[i + 1].trim())) {
          const parseRow = (r) => r.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => renderInline(c.trim()));
          const headers = parseRow(line);
          i += 2;
          let tbl = "<table><thead><tr>" + headers.map((h) => `<th>${h}</th>`).join("") + "</tr></thead><tbody>";
          while (i < lines.length && lines[i].trim().startsWith("|")) {
            const cells = parseRow(lines[i]);
            tbl += "<tr>" + cells.map((c) => `<td>${c}</td>`).join("") + "</tr>";
            i++;
          }
          tbl += "</tbody></table>";
          out.push(tbl);
          continue;
        }
        if (/^[-*] /.test(line.trim())) {
          let ul = "<ul>";
          while (i < lines.length && /^[-*] /.test(lines[i].trim())) {
            ul += `<li>${renderInline(lines[i].trim().slice(2))}</li>`;
            i++;
          }
          ul += "</ul>";
          out.push(ul);
          continue;
        }
        if (/^### /.test(line)) out.push(`<h4>${renderInline(line.slice(4))}</h4>`);
        else if (/^## /.test(line)) out.push(`<h3>${renderInline(line.slice(3))}</h3>`);
        else if (/^# /.test(line)) out.push(`<h2>${renderInline(line.slice(2))}</h2>`);
        else out.push(renderInline(line));
        i++;
      }
      return out.join("\n");
    }

    function syncEmptyState() {
      emptyHero.style.display = messagesEl.children.length === 0 ? "block" : "none";
    }

    function addMsg(text, cls, metrics = null, opts = {}) {
      if (cls === "user") {
        lastUserPrompt = text;
      }
      const div = document.createElement("div");
      div.className = "msg " + cls;
      if (cls === "aura") {
        div.innerHTML = renderMarkdown(text);
        div.querySelectorAll(".copy-code-btn").forEach((btn) => {
          btn.addEventListener("click", async () => {
            const code = btn.nextElementSibling?.textContent || "";
            await navigator.clipboard.writeText(code).catch(() => {});
            btn.textContent = "Copied";
            setTimeout(() => { btn.textContent = "Copy"; }, 1200);
          });
        });
        const retryPrompt = opts.prompt || lastUserPrompt;
        if (metrics || opts.cloud) {
          const footer = document.createElement("div");
          footer.className = "aura-footer";
          const m = document.createElement("div");
          m.className = "metrics";
          m.textContent = metrics || "";
          footer.appendChild(m);
          if (opts.cloud && retryPrompt) {
            const rbtn = document.createElement("button");
            rbtn.type = "button";
            rbtn.className = "retry-btn";
            rbtn.innerHTML = "&#8635;";
            rbtn.title = "Retry / continue Cloud response";
            rbtn.disabled = sendBtn.disabled;
            rbtn.addEventListener("click", () => {
              if (!sendBtn.disabled) sendPrompt(retryPrompt);
            });
            footer.appendChild(rbtn);
          }
          div.appendChild(footer);
        }
      } else {
        div.textContent = text;
        if (metrics) {
          const m = document.createElement("div");
          m.className = "metrics";
          m.textContent = metrics;
          div.appendChild(m);
        }
      }
      messagesEl.appendChild(div);
      syncEmptyState();
      const stage = document.getElementById("stage");
      stage.scrollTop = stage.scrollHeight;
    }

    function setBusy(busy, label = null) {
      isBusyState = Boolean(busy);
      syncActionBtn();
      document.querySelectorAll(".retry-btn").forEach((b) => { b.disabled = busy; });
      statusDot.className = "dot" + (busy ? " busy" : "");
      statusText.textContent = label || (busy ? "Running..." : "Ready");
    }

    function populateModelSelect(s) {
      const installed = s.available_models || [];
      const cloudModels = s.available_cloud_models || [];
      const seq = s.local_roles || s.roles || {};
      const currentVal = s.low_vram ? "low_vram" : (s.model_override ? `model:${s.model_override}` : "auto");
      modelSelect.innerHTML = "";
      const opts = [
        { value: "auto", label: `AURA Sequential (${seq.analysis || "4B"} + ${seq.coding || "7B"})` },
        { value: "low_vram", label: "AURA Low-VRAM (qwen2.5-coder:7b)" },
      ];
      for (const m of installed) {
        opts.push({ value: `model:${m}`, label: m });
      }
      for (const cm of cloudModels) {
        const nice = cm === "cloud:auto"
          ? "Auto Failover"
          : cm.startsWith("cloud:custom/")
          ? `FreeLLMAPI (${cm.slice(13)})`
          : cm.slice(6);
        opts.push({ value: `model:${cm}`, label: `Cloud · ${nice}` });
      }
      opts.push({ value: "cloud:configure", label: "Configure Free Cloud API Key..." });
      for (const o of opts) {
        const el = document.createElement("option");
        el.value = o.value;
        el.textContent = o.label;
        modelSelect.appendChild(el);
      }
      modelSelect.value = currentVal;
      if (!modelSelect.value) modelSelect.value = "auto";
      cloudHeavyToggle.checked = Boolean(s.cloud_for_heavy);
    }

    function renderStatus(s) {
      activeChatId = s.active_chat_id || activeChatId;
      currentWorkspace = s.workspace || "";
      const hasWs = s.workspace_selected !== false && Boolean(s.workspace);
      wsNameEl.textContent = hasWs ? (s.workspace_name || "Workspace") : "Select workspace";
      const isCloud = (s.model_override || "").startsWith("cloud:");
      const engineHint = isCloud ? "Free Cloud API · 0 local VRAM" : "Sequential VRAM lock";
      wsPathHintEl.textContent = hasWs
        ? `${s.workspace} · ${s.mode.toUpperCase()} · ${engineHint}`
        : `No workspace selected · ${engineHint}`;
      roBtn.className = "seg-btn " + (s.read_only ? "active-ro" : "");
      rwBtn.className = "seg-btn " + (s.read_only ? "" : "active-rw");
      toolsInfoEl.textContent = `${s.tools_count} tools (${(s.servers || []).join(", ")})`;
      populateModelSelect(s);
      if (voiceSelect && s.voice && s.voice.persona) {
        voiceSelect.value = s.voice.persona;
      }
      setBusy(s.busy);
    }

    const deleteChatModal = document.getElementById("deleteChatModal");
    const deleteChatText = document.getElementById("deleteChatText");
    let pendingDeleteChatId = null;

    document.getElementById("deleteChatCancel").addEventListener("click", () => {
      pendingDeleteChatId = null;
      deleteChatModal.classList.remove("open");
    });

    document.getElementById("deleteChatConfirm").addEventListener("click", async () => {
      const targetId = pendingDeleteChatId;
      pendingDeleteChatId = null;
      deleteChatModal.classList.remove("open");
      if (!targetId) return;
      await fetch(`/api/chats/${targetId}`, { method: "DELETE" });
      if (activeChatId === targetId) {
        messagesEl.innerHTML = "";
        syncEmptyState();
      }
      refreshChats();
    });

    const chatActionMenu = document.getElementById("chatActionMenu");
    function closeChatMenu() {
      chatActionMenu.classList.remove("open");
      chatActionMenu.innerHTML = "";
    }
    document.addEventListener("click", (e) => {
      if (!chatActionMenu.contains(e.target)) closeChatMenu();
    });

    function openChatMenu(anchorBtn, c, projects) {
      closeChatMenu();
      const label = document.createElement("div");
      label.className = "chat-menu-label";
      label.textContent = "Move to project";
      chatActionMenu.appendChild(label);

      if (c.project) {
        const toNormal = document.createElement("button");
        toNormal.type = "button";
        toNormal.className = "chat-menu-item";
        toNormal.textContent = "Normal Chats";
        toNormal.addEventListener("click", async (e) => {
          e.stopPropagation();
          closeChatMenu();
          await postJson(`/api/chats/${c.id}/project`, { project: "" });
          refreshChats();
        });
        chatActionMenu.appendChild(toNormal);
      }

      const targetProjects = projects.filter((p) => p !== (c.project || ""));
      if (targetProjects.length === 0 && !c.project) {
        const none = document.createElement("div");
        none.style.cssText = "padding:4px 8px;font-size:11.5px;color:var(--muted);";
        none.textContent = "Create a project first (+ Project)";
        chatActionMenu.appendChild(none);
      } else {
        for (const p of targetProjects) {
          const item = document.createElement("button");
          item.type = "button";
          item.className = "chat-menu-item";
          item.textContent = `📁 ${p}`;
          item.addEventListener("click", async (e) => {
            e.stopPropagation();
            closeChatMenu();
            await postJson(`/api/chats/${c.id}/project`, { project: p });
            refreshChats();
          });
          chatActionMenu.appendChild(item);
        }
      }

      const sep = document.createElement("div");
      sep.className = "chat-menu-sep";
      chatActionMenu.appendChild(sep);

      const delItem = document.createElement("button");
      delItem.type = "button";
      delItem.className = "chat-menu-item danger";
      delItem.textContent = "Delete chat";
      delItem.addEventListener("click", (e) => {
        e.stopPropagation();
        closeChatMenu();
        pendingDeleteChatId = c.id;
        deleteChatText.textContent = `Deleting "${c.title}" will remove all its saved memory and context from SSD. Delete permanently?`;
        deleteChatModal.classList.add("open");
      });
      chatActionMenu.appendChild(delItem);

      const rect = anchorBtn.getBoundingClientRect();
      chatActionMenu.classList.add("open");
      const top = Math.min(rect.bottom + 4, window.innerHeight - chatActionMenu.offsetHeight - 10);
      const left = Math.min(rect.left, window.innerWidth - chatActionMenu.offsetWidth - 10);
      chatActionMenu.style.top = Math.max(8, top) + "px";
      chatActionMenu.style.left = Math.max(8, left) + "px";
    }

    function formatLastUsed(ts, compact = false) {
      if (!ts) return "";
      const d = new Date(ts * 1000);
      if (Number.isNaN(d.getTime())) return "";
      const now = new Date();
      const sameYear = d.getFullYear() === now.getFullYear();
      if (compact) {
        return d.toLocaleDateString(undefined, sameYear ? { month: "short", day: "numeric" } : { year: "numeric", month: "short", day: "numeric" });
      }
      return d.toLocaleString(
        undefined,
        sameYear
          ? { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }
          : { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }
      );
    }

    const chatSearchInput = document.getElementById("chatSearchInput");
    let cachedChatsData = { chats: [], projects: [], project_timestamps: {} };

    function buildChatRow(c, projects) {
      const row = document.createElement("div");
      row.className = "chat-item" + (c.id === activeChatId ? " active" : "");
      const meta = document.createElement("div");
      meta.className = "chat-meta";
      const title = document.createElement("div");
      title.className = "chat-title";
      title.textContent = c.title;
      const sub = document.createElement("div");
      sub.className = "chat-sub";
      const folder = c.workspace ? ((c.workspace || "").split(/[\\/]/).filter(Boolean).pop() || c.workspace) : "";
      const when = formatLastUsed(c.updated_at);
      sub.textContent = [folder, `${c.message_count} msgs`, when].filter(Boolean).join(" · ");
      if (c.updated_at) sub.title = `Last used: ${new Date(c.updated_at * 1000).toLocaleString()}`;
      meta.appendChild(title);
      meta.appendChild(sub);
      meta.addEventListener("click", () => loadChat(c.id));
      row.appendChild(meta);

      const more = document.createElement("button");
      more.type = "button";
      more.className = "more-btn";
      more.innerHTML = "&#8943;";
      more.title = "Chat options (Move to project / Delete)";
      more.addEventListener("click", (e) => {
        e.stopPropagation();
        openChatMenu(more, c, projects);
      });
      row.appendChild(more);
      return row;
    }

    function renderSidebarLists() {
      const q = (chatSearchInput?.value || "").trim().toLowerCase();
      const projects = cachedChatsData.projects || [];
      const projTs = cachedChatsData.project_timestamps || {};
      const chats = cachedChatsData.chats || [];
      const matchesChat = (c) => {
        if (!q) return true;
        const hay = `${c.title || ""} ${c.project || ""} ${c.workspace || ""} ${c.search_text || ""}`.toLowerCase();
        return hay.includes(q);
      };
      chatListEl.innerHTML = "";

      const visibleProjects = [];
      for (const proj of projects) {
        const projChats = chats.filter((c) => (c.project || "") === proj);
        const nameMatches = !q || proj.toLowerCase().includes(q);
        const matchedChats = nameMatches ? projChats : projChats.filter(matchesChat);
        if (!q || nameMatches || matchedChats.length > 0) {
          visibleProjects.push({ proj, matchedChats });
        }
      }

      if (visibleProjects.length > 0) {
        const pTag = document.createElement("div");
        pTag.className = "section-tag";
        pTag.innerHTML = `<span>Projects</span><span>${visibleProjects.length}</span>`;
        chatListEl.appendChild(pTag);

        for (const { proj, matchedChats } of visibleProjects) {
          const isCollapsed = !q && collapsedProjects.has(proj);
          const group = document.createElement("div");
          group.className = "proj-group" + (isCollapsed ? " collapsed" : "");

          const head = document.createElement("div");
          head.className = "proj-head";
          const caret = isCollapsed ? "▸" : "▾";
          const whenShort = formatLastUsed(projTs[proj], true);
          const countLabel = whenShort ? `${matchedChats.length} · ${whenShort}` : `${matchedChats.length}`;
          head.innerHTML = `<span>${caret}</span><span class="proj-name"></span><span class="proj-count">${countLabel}</span>`;
          head.querySelector(".proj-name").textContent = proj;
          if (projTs[proj]) head.title = `Last used: ${new Date(projTs[proj] * 1000).toLocaleString()}`;

          const addSub = document.createElement("button");
          addSub.className = "proj-mini-btn";
          addSub.textContent = "+";
          addSub.title = "New sub-chat in project";
          addSub.addEventListener("click", async (e) => {
            e.stopPropagation();
            const created = await postJson("/api/chats", { project: proj });
            if (created) {
              collapsedProjects.delete(proj);
              messagesEl.innerHTML = "";
              syncEmptyState();
              renderStatus(created.status);
              refreshChats();
            }
          });

          const delProj = document.createElement("button");
          delProj.className = "proj-mini-btn";
          delProj.textContent = "×";
          delProj.title = "Remove project folder (keeps chats)";
          delProj.addEventListener("click", async (e) => {
            e.stopPropagation();
            await fetch(`/api/projects/${encodeURIComponent(proj)}`, { method: "DELETE" });
            refreshChats();
          });

          head.appendChild(addSub);
          head.appendChild(delProj);
          head.addEventListener("click", () => {
            if (collapsedProjects.has(proj)) collapsedProjects.delete(proj);
            else collapsedProjects.add(proj);
            renderSidebarLists();
          });

          const listWrap = document.createElement("div");
          listWrap.className = "proj-chats";
          if (matchedChats.length === 0) {
            const empty = document.createElement("div");
            empty.className = "proj-empty";
            empty.textContent = "No sub-chats yet";
            listWrap.appendChild(empty);
          } else {
            for (const c of matchedChats) {
              listWrap.appendChild(buildChatRow(c, projects));
            }
          }
          group.appendChild(head);
          group.appendChild(listWrap);
          chatListEl.appendChild(group);
        }
      }

      const normalChats = chats.filter((c) => !(c.project || "").trim() && matchesChat(c));
      if (!q || normalChats.length > 0) {
        const nTag = document.createElement("div");
        nTag.className = "section-tag";
        nTag.innerHTML = `<span>Normal Chats</span><span>${normalChats.length}</span>`;
        chatListEl.appendChild(nTag);
        for (const c of normalChats) {
          chatListEl.appendChild(buildChatRow(c, projects));
        }
      }
      if (q && visibleProjects.length === 0 && normalChats.length === 0) {
        const empty = document.createElement("div");
        empty.className = "proj-empty";
        empty.style.padding = "12px 10px";
        empty.textContent = "No matching projects or chats";
        chatListEl.appendChild(empty);
      }
    }

    chatSearchInput.addEventListener("input", renderSidebarLists);

    async function refreshChats() {
      const res = await fetch("/api/chats");
      if (!res.ok) return;
      const data = await res.json();
      activeChatId = data.active_chat_id;
      cachedChatsData = data;
      renderSidebarLists();
    }

    async function loadChat(chatId) {
      const res = await fetch(`/api/chats/${chatId}`);
      if (!res.ok) return;
      const data = await res.json();
      activeChatId = data.chat.id;
      messagesEl.innerHTML = "";
      lastUserPrompt = "";
      let prevPrompt = "";
      for (const m of data.chat.messages || []) {
        if (m.role === "user") prevPrompt = m.text;
        addMsg(m.text, m.role, m.metrics || null, { cloud: Boolean(m.cloud), prompt: prevPrompt });
      }
      syncEmptyState();
      if (data.status) renderStatus(data.status);
      refreshChats();
    }

    async function postJson(url, payload) {
      try {
        const res = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: payload ? JSON.stringify(payload) : undefined,
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          addMsg("Error: " + (data.detail || `HTTP ${res.status}`), "meta");
          return null;
        }
        return data;
      } catch (err) {
        addMsg("Network error: " + err.message, "meta");
        return null;
      }
    }

    function connectWs() {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      ws = new WebSocket(`${proto}//${location.host}/ws/chat`);
      ws.onmessage = (ev) => {
        const data = JSON.parse(ev.data);
        if (data.type === "status") {
          setBusy(true, data.message || "Working...");
          updateLiveProgress(data.message || "Working...");
        } else if (data.type === "progress") {
          const label = data.message || [data.stage, data.tool, data.status].filter(Boolean).join(" · ");
          setBusy(true, label);
          updateLiveProgress(label);
        } else if (data.type === "result") {
          clearLiveProgress(true);
          addMsg(data.answer, "aura", data.metrics, { cloud: Boolean(data.cloud), prompt: lastUserPrompt });
          speakAnswerInBrowser(data.answer);
          if (data.status) renderStatus(data.status);
          refreshChats();
          setBusy(false);
        } else if (data.type === "stopped") {
          clearLiveProgress(false);
          addMsg(data.message || "Stopped by user.", "meta");
          if (data.status) renderStatus(data.status);
          setBusy(false, "Stopped");
        } else if (data.type === "error") {
          clearLiveProgress(false);
          addMsg("Error: " + data.message, "meta");
          setBusy(false);
        }
      };
      ws.onclose = () => {
        ws = null;
        setTimeout(connectWs, 2000);
      };
    }

    async function sendPrompt(promptText) {
      const prompt = promptText.trim();
      if (!prompt) return;
      const agentRole = agentSelect ? agentSelect.value : "auto";
      addMsg(prompt, "user");
      setBusy(true, "Working...");
      updateLiveProgress("Working...");
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ prompt, agent_role: agentRole }));
      } else {
        const data = await postJson("/api/chat", { prompt, agent_role: agentRole });
        clearLiveProgress(true);
        if (data) {
          if (data.stopped) {
            addMsg(data.answer || "Stopped by user.", "meta");
          } else {
            addMsg(data.answer, "aura", data.metrics, { cloud: Boolean(data.cloud), prompt });
            speakAnswerInBrowser(data.answer);
          }
          if (data.status) renderStatus(data.status);
          refreshChats();
        }
        setBusy(false, data && data.stopped ? "Stopped" : null);
      }
    }

    let resizeRaf = 0;
    function schedulePromptResize() {
      if (resizeRaf) return;
      resizeRaf = requestAnimationFrame(() => {
        resizeRaf = 0;
        if (!promptEl.value) {
          promptEl.style.height = "26px";
          promptEl.style.overflowY = "hidden";
          return;
        }
        const prev = promptEl.offsetHeight;
        promptEl.style.height = "26px";
        const target = Math.max(26, Math.min(promptEl.scrollHeight, 160));
        if (target !== prev) promptEl.style.height = target + "px";
        else promptEl.style.height = prev + "px";
        promptEl.style.overflowY = promptEl.scrollHeight > 160 ? "auto" : "hidden";
      });
    }

    document.getElementById("composer").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (isBusyState || isVoicePlaying) {
        await stopExecution();
        return;
      }
      const val = promptEl.value;
      promptEl.value = "";
      promptEl.style.height = "26px";
      promptEl.style.overflowY = "hidden";
      await sendPrompt(val);
    });

    promptEl.addEventListener("input", schedulePromptResize);
    promptEl.addEventListener("paste", schedulePromptResize);

    promptEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        document.getElementById("composer").requestSubmit();
      }
    });

    document.querySelectorAll(".chip").forEach((btn) => {
      btn.addEventListener("click", () => sendPrompt(btn.dataset.prompt || ""));
    });

    document.getElementById("toggleSidebarBtn").addEventListener("click", () => {
      sidebar.classList.toggle("collapsed");
    });

    document.getElementById("newChatBtn").addEventListener("click", async () => {
      const data = await postJson("/api/chats", {});
      if (data) {
        messagesEl.innerHTML = "";
        syncEmptyState();
        renderStatus(data.status);
        refreshChats();
      }
    });

    document.getElementById("newProjectBtn").addEventListener("click", () => {
      projectModalInput.value = "";
      projectModal.classList.add("open");
      projectModalInput.focus();
    });

    document.getElementById("projectModalCancel").addEventListener("click", () => {
      projectModal.classList.remove("open");
    });

    document.getElementById("projectModalSave").addEventListener("click", async () => {
      const name = projectModalInput.value.trim();
      if (!name) return;
      projectModal.classList.remove("open");
      const data = await postJson("/api/projects", { name });
      if (data) refreshChats();
    });

    projectModalInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        document.getElementById("projectModalSave").click();
      } else if (e.key === "Escape") {
        projectModal.classList.remove("open");
      }
    });

    document.getElementById("changeWsBtn").addEventListener("click", () => {
      wsModalInput.value = currentWorkspace;
      wsModal.classList.add("open");
      wsModalInput.focus();
    });

    document.getElementById("wsModalCancel").addEventListener("click", () => {
      wsModal.classList.remove("open");
    });

    document.getElementById("wsModalSave").addEventListener("click", async () => {
      const path = wsModalInput.value.trim();
      if (!path) return;
      wsModal.classList.remove("open");
      const data = await postJson("/api/workspace", { path });
      if (data) {
        renderStatus(data);
        addMsg("Workspace switched to " + data.workspace + " (RO)", "meta");
      }
    });

    wsModalInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        document.getElementById("wsModalSave").click();
      } else if (e.key === "Escape") {
        wsModal.classList.remove("open");
      }
    });

    roBtn.addEventListener("click", async () => {
      const data = await postJson("/api/mode", { mode: "ro" });
      if (data) {
        renderStatus(data);
        addMsg("Access mode: Read-Only (ro)", "meta");
      }
    });

    rwBtn.addEventListener("click", () => {
      rwModal.classList.add("open");
    });

    document.getElementById("rwModalCancel").addEventListener("click", () => {
      rwModal.classList.remove("open");
    });

    document.getElementById("rwModalConfirm").addEventListener("click", async () => {
      rwModal.classList.remove("open");
      const data = await postJson("/api/mode", { mode: "rw" });
      if (data) {
        renderStatus(data);
        addMsg("Access mode: Read-Write (rw)", "meta");
      }
    });

    modelSelect.addEventListener("change", async () => {
      const val = modelSelect.value;
      if (val === "cloud:configure") {
        cloudModal.classList.add("open");
        cloudKeyInput.focus();
        const statusRes = await fetch("/api/status");
        if (statusRes.ok) renderStatus(await statusRes.json());
        return;
      }
      const payload =
        val === "low_vram"
          ? { low_vram: true, model_override: null }
          : val.startsWith("model:")
          ? { low_vram: false, model_override: val.slice(6) }
          : { low_vram: false, model_override: null };
      const data = await postJson("/api/model", payload);
      if (data) {
        renderStatus(data);
        addMsg("Model switched: " + modelSelect.options[modelSelect.selectedIndex].text, "meta");
      }
    });

    if (voiceSelect) {
      voiceSelect.addEventListener("change", async () => {
        const persona = voiceSelect.value;
        if (persona === "off" && currentVoiceAudio) {
          currentVoiceAudio.pause();
          currentVoiceAudio = null;
        }
        const res = await postJson("/api/voice/config", { persona, auto_speak: persona !== "off" });
        if (res && res.status) renderStatus(res.status);
        addMsg("Voice persona: " + voiceSelect.options[voiceSelect.selectedIndex].text, "meta");
      });
    }

    const appsModal = document.getElementById("appsModal");
    const appsCornerBtn = document.getElementById("appsCornerBtn");
    const appsGmailToggle = document.getElementById("appsGmailToggle");
    const appsCalToggle = document.getElementById("appsCalToggle");
    const appsGmailBadge = document.getElementById("appsGmailBadge");
    const appsCalBadge = document.getElementById("appsCalBadge");
    const appsSaveState = document.getElementById("appsSaveState");
    function renderAppsState(apps) {
      if (!apps) return;
      const g = apps.gmail || {};
      const c = apps.calendar || {};
      if (appsGmailToggle) appsGmailToggle.checked = !!g.enabled;
      if (appsCalToggle) appsCalToggle.checked = !!c.enabled;
      if (appsGmailBadge) appsGmailBadge.textContent = g.connected ? ("Connected" + (g.account ? " · " + g.account : "")) : (g.configured ? "Paused" : "Disconnected");
      if (appsCalBadge) appsCalBadge.textContent = c.connected ? ("Connected (" + (c.source || "sync") + ")") : "Local";
      const n = (g.connected ? 1 : 0) + (c.connected ? 1 : 0);
      if (appsCornerBtn) appsCornerBtn.textContent = n ? ("⊞ Apps · " + n) : "⊞ Apps";
    }
    async function refreshAppsState() {
      try {
        const res = await fetch("/api/apps/status");
        if (res.ok) renderAppsState((await res.json()).apps);
      } catch {}
    }
    if (appsCornerBtn && appsModal) {
      appsCornerBtn.addEventListener("click", async () => {
        if (appsSaveState) appsSaveState.textContent = "";
        appsModal.classList.add("open");
        await refreshAppsState();
      });
      document.getElementById("appsModalClose").addEventListener("click", () => appsModal.classList.remove("open"));
      appsGmailToggle.addEventListener("change", async () => {
        const res = await postJson("/api/apps/connect", { gmail_enabled: appsGmailToggle.checked });
        if (res && res.apps) renderAppsState(res.apps);
      });
      appsCalToggle.addEventListener("change", async () => {
        const res = await postJson("/api/apps/connect", { calendar_enabled: appsCalToggle.checked });
        if (res && res.apps) renderAppsState(res.apps);
      });
      document.getElementById("appsModalSave").addEventListener("click", async () => {
        const payload = {
          gmail_enabled: appsGmailToggle.checked,
          calendar_enabled: appsCalToggle.checked,
        };
        const ga = document.getElementById("appsGmailAddr").value.trim();
        const gp = document.getElementById("appsGmailPass").value.trim();
        const cu = document.getElementById("appsCalIcalUrl").value.trim();
        if (ga) payload.gmail_address = ga;
        if (gp) payload.gmail_app_password = gp;
        if (cu) payload.gcal_ical_url = cu;
        const res = await postJson("/api/apps/connect", payload);
        if (res && res.apps) {
          renderAppsState(res.apps);
          document.getElementById("appsGmailPass").value = "";
          if (appsSaveState) appsSaveState.textContent = "Saved";
        }
      });
      refreshAppsState();
    }

    cloudProviderSelect.addEventListener("change", () => {
      const isCustom = cloudProviderSelect.value === "custom";
      cloudBaseUrlInput.style.display = isCustom ? "block" : "none";
      cloudModelInput.placeholder = isCustom
        ? "Model ID (optional, e.g. auto)"
        : (cloudProviderSelect.value === "gemini" ? "Model ID (default: gemini-3.8-flash)" : "Model ID (optional)");
    });

    document.getElementById("cloudModalCancel").addEventListener("click", () => {
      cloudModal.classList.remove("open");
    });

    document.getElementById("cloudModalSave").addEventListener("click", async () => {
      const api_key = cloudKeyInput.value.trim();
      if (!api_key) return;
      const data = await postJson("/api/cloud-key", {
        provider: cloudProviderSelect.value,
        api_key,
        base_url: cloudBaseUrlInput.value.trim() || null,
        model: cloudModelInput.value.trim() || null,
        use_for_heavy: cloudHeavyToggle.checked,
      });
      if (data && data.status) {
        cloudKeyInput.value = "";
        cloudModal.classList.remove("open");
        renderStatus(data.status);
        addMsg(`Cloud API key saved to ${data.saved_to} (persists across reboots, 0 idle RAM).`, "meta");
      }
    });

    document.getElementById("resetBtn").addEventListener("click", async () => {
      const data = await postJson("/api/reset");
      if (data) {
        renderStatus(data);
        addMsg("Session memory and result cache cleared.", "meta");
      }
    });

    document.getElementById("unloadVramBtn").addEventListener("click", async () => {
      const data = await postJson("/api/vram/unload");
      if (data) {
        if (data.status) renderStatus(data.status);
        const list = (data.unloaded && data.unloaded.length) ? data.unloaded.join(", ") : "GPU VRAM free";
        addMsg("VRAM unloaded: " + list, "meta");
      }
    });

    document.getElementById("shutdownBtn").addEventListener("click", async () => {
      addMsg("Unloading VRAM and shutting down AURA V4...", "meta");
      await postJson("/api/shutdown");
      statusText.textContent = "Stopped";
      statusDot.className = "dot";
    });

    (async () => {
      const res = await fetch("/api/status");
      if (res.ok) renderStatus(await res.json());
      await refreshChats();
      connectWs();
    })();
  </script>
</body>
</html>
"""


_ISLAND_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aura V4 · Ultron Dynamic Island</title>""" + _VENDOR_HEAD_SCRIPTS + r"""
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Manrope:wght@400;500;600;700&family=Michroma&family=Syncopate:wght@700&display=swap" rel="stylesheet">
<style>
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0; min-height: 100vh;
    background:
      radial-gradient(900px 520px at 50% -4%, rgba(70,170,230,0.14), transparent 70%),
      radial-gradient(700px 500px at 92% 100%, rgba(70,170,230,0.07), transparent 70%),
      radial-gradient(700px 460px at 4% 62%, rgba(110,140,190,0.16), transparent 70%),
      linear-gradient(165deg, #2B313C 0%, #1D2129 55%, #211D20 100%);
    font-family: 'Manrope', system-ui, sans-serif;
    color: #E8EDF4;
    -webkit-font-smoothing: antialiased;
  }
  button { font-family: inherit; cursor: pointer; transition: filter 0.14s, transform 0.1s; }
  button:hover { filter: brightness(1.12); }
  button:active { transform: translateY(1px); }
  .wrap { max-width: 1280px; margin: 0 auto; padding: 34px 28px 48px; display: flex; flex-direction: column; gap: 28px; }
  .pill-btn-arc {
    border: 1px solid #6FCBF4;
    background: linear-gradient(180deg, #237BB5, #134B78);
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.5), 0 3px 10px rgba(0,0,0,0.4), 0 0 14px rgba(70,170,230,0.3);
    color: #F2F8FF; font-weight: 600; border-radius: 999px;
  }
  .pill-btn-steel {
    border: 1px solid rgba(150,165,185,0.45);
    background: linear-gradient(180deg, #4B5260, #333945);
    box-shadow: inset 0 1px 0 rgba(215,228,250,0.18), 0 3px 8px rgba(0,0,0,0.4);
    color: #DCE4EE; border-radius: 999px;
  }
  .pill-btn-danger {
    border: 1px solid #E11D48;
    background: linear-gradient(180deg, #4B1D2D, #32131E);
    box-shadow: inset 0 1px 0 rgba(253,164,175,0.2), 0 3px 8px rgba(0,0,0,0.45);
    color: #FDA4AF; font-weight: 600; border-radius: 999px;
  }
  .dropdown-menu { display: none; }
  .dropdown-menu.open { display: flex; }
  .agent-chip {
    height: 32px; padding: 0 13px; border-radius: 999px;
    border: 1px solid rgba(150,165,185,0.28);
    background: linear-gradient(180deg, #353B46, #2B3039);
    color: #C3CCD8; font-size: 12px; font-family: 'JetBrains Mono', monospace;
  }
  .agent-chip.active {
    border-color: #6FCBF4;
    background: linear-gradient(180deg, rgba(92,195,242,0.26), rgba(70,170,230,0.12));
    color: #B4E1F8;
  }
  .tab-pill {
    height: 36px; padding: 0 18px; border-radius: 999px;
    border: 1px solid rgba(150,165,185,0.28); background: transparent;
    color: #C3CCD8; font-size: 13px;
  }
  .tab-pill.active {
    border-color: #6FCBF4;
    background: linear-gradient(180deg, rgba(92,195,242,0.26), rgba(70,170,230,0.14));
    color: #B4E1F8; font-weight: 600;
  }
  .chat-scroll { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; padding-right: 4px; }
  .model-opt { padding: 9px 12px; border-radius: 12px; font-size: 13px; color: #E8EDF4; cursor: pointer; }
  .model-opt:hover { background: rgba(92,195,242,0.16); }
  .model-opt.active { background: linear-gradient(180deg, rgba(92,195,242,0.24), rgba(70,170,230,0.12)); border: 1px solid #6FCBF4; color: #B4E1F8; }
</style>
</head>
<body>
<div class="wrap">
  <!-- Title -->
  <div style="display:flex; align-items:flex-end; justify-content:space-between; flex-wrap:wrap; gap:12px">
    <div style="display:flex; flex-direction:column; gap:8px">
      <div style="font-family:'Syncopate',sans-serif; font-size:28px; font-weight:700; letter-spacing:0.18em; text-transform:uppercase; background:linear-gradient(180deg,#F2F6FB 0%,#8795AA 100%); -webkit-background-clip:text; background-clip:text; color:transparent">Aura V4 · Ultron</div>
      <div style="font-size:14px; color:#98A4B5">Dynamic island live console. Dark vibranium steel with a cold arc-blue core.</div>
    </div>
    <div style="font-family:'JetBrains Mono',monospace; font-size:12px; color:#98A4B5; text-align:right; line-height:1.7">pill radius 999px · panel radius 40px<br>steel hairline 1px #96A5B9</div>
  </div>

  <!-- Row 1: Compact & Shuttered -->
  <div style="display:flex; gap:40px; align-items:flex-start; flex-wrap:wrap">
    <!-- 01 Compact -->
    <div style="display:flex; flex-direction:column; gap:12px">
      <div style="font-family:'Michroma',sans-serif; font-size:10px; letter-spacing:0.2em; text-transform:uppercase; color:#8FA0B5">01 · Compact island</div>
      <div style="position:relative; display:flex; align-items:center; gap:14px; width:660px; max-width:100%; height:68px; padding:0 14px 0 12px; border-radius:999px; background:repeating-linear-gradient(0deg,rgba(255,255,255,0.018) 0px,rgba(255,255,255,0.018) 1px,rgba(0,0,0,0) 1px,rgba(0,0,0,0) 3px),linear-gradient(180deg,#464D5A 0%,#2E343E 45%,#21252C 100%); border:1px solid rgba(150,165,185,0.55); box-shadow:0 18px 40px rgba(0,0,0,0.5),0 0 0 1px rgba(0,0,0,0.55),inset 0 1px 0 rgba(215,228,250,0.22),inset 0 -1px 0 rgba(0,0,0,0.6)">
        <div style="position:absolute; top:-1px; left:50%; margin-left:-34px; width:68px; height:7px; clip-path:polygon(0 0,100% 0,88% 100%,12% 100%); background:linear-gradient(180deg,#DCE4EE,#6B7587)"></div>
        <div style="position:absolute; top:2px; left:50%; margin-left:-14px; width:28px; height:2px; border-radius:2px; background:#5CC3F2; box-shadow:0 0 8px rgba(70,170,230,0.9)"></div>
        <div style="position:relative; flex:none; width:44px; height:44px; border-radius:50%; background:conic-gradient(from 200deg,#4A5160,#C9D3E0,#6B7587,#E6EDF5,#4A5160,#98A5B8,#4A5160); box-shadow:0 2px 6px rgba(0,0,0,0.6),0 0 16px rgba(92,195,242,0.32)">
          <div style="position:absolute; inset:4px; border-radius:50%; background:#13161B"></div>
          <div style="position:absolute; inset:9px; border-radius:50%; border:1px solid rgba(92,195,242,0.6); background:radial-gradient(circle at 50% 50%,#EAF8FF 0%,#5CC3F2 12%,#0F3A5C 30%,#121316 62%)"></div>
        </div>
        <div style="flex:1; min-width:0; display:flex; align-items:center; justify-content:center">
          <svg width="150" height="36" viewBox="0 0 150 36" aria-hidden="true">
            <defs>
              <linearGradient id="gfA" x1="0" x2="1" y1="0" y2="0"><stop offset="0" stop-color="#1F6FA8" stop-opacity="0"/><stop offset="0.25" stop-color="#1F6FA8"/><stop offset="0.5" stop-color="#D2EEFC"/><stop offset="0.75" stop-color="#1F6FA8"/><stop offset="1" stop-color="#1F6FA8" stop-opacity="0"/></linearGradient>
              <radialGradient id="rgA"><stop offset="0" stop-color="#5CC3F2" stop-opacity="0.9"/><stop offset="1" stop-color="#5CC3F2" stop-opacity="0"/></radialGradient>
            </defs>
            <path id="filCompactDim" d="" fill="none" stroke="url(#gfA)" stroke-width="1" opacity="0.45"/>
            <path id="filCompactMain" d="" fill="none" stroke="url(#gfA)" stroke-width="1.6" stroke-linecap="round"/>
            <circle cx="75" cy="18" r="8" fill="url(#rgA)"/>
            <circle cx="75" cy="18" r="1.8" fill="#E6F6FF"/>
          </svg>
        </div>
        <button id="compactModelBtn" style="flex:none; display:flex; align-items:center; gap:8px; height:38px; padding:0 14px; border-radius:999px; border:1px solid rgba(0,0,0,0.55); background:linear-gradient(180deg,#14171C,#1B1F26); color:#DCE4EE; font-size:13px; font-weight:500">qwen3.5:4b</button>
        <div style="flex:none; display:flex; align-items:center; gap:8px; font-size:13px; color:#BAC4D1">
          Mic
          <button id="compactMicSwitch" role="switch" aria-checked="true" style="position:relative; width:40px; height:22px; padding:0; border-radius:999px; border:1px solid rgba(0,0,0,0.55); background:linear-gradient(180deg,#14171C,#1B1F26)">
            <span id="compactMicThumb" style="position:absolute; top:2px; right:3px; width:16px; height:16px; border-radius:50%; background:linear-gradient(180deg,#8FD3F4,#2E8FD0); box-shadow:0 0 9px rgba(70,170,230,0.7)"></span>
          </button>
        </div>
        <button onclick="stopAura()" class="pill-btn-steel" style="flex:none; height:38px; padding:0 18px; font-size:13px; font-weight:600">Stop</button>
        <button onclick="setShutter(true)" aria-label="Shutter" title="Shutter Island" class="pill-btn-steel" style="flex:none; width:38px; height:38px; padding:0; display:flex; align-items:center; justify-content:center">◐</button>
        <button id="compactShutdownBtn" onclick="shutdownAura()" aria-label="Shut Down AURA" title="Shut Down AURA V4 & Free VRAM" class="pill-btn-danger" style="flex:none; width:38px; height:38px; padding:0; display:flex; align-items:center; justify-content:center">⏻</button>
      </div>
    </div>

    <!-- 02 Shuttered -->
    <div style="display:flex; flex-direction:column; gap:12px">
      <div style="font-family:'Michroma',sans-serif; font-size:10px; letter-spacing:0.2em; text-transform:uppercase; color:#8FA0B5">02 · Shuttered standby</div>
      <div style="display:flex; align-items:center; justify-content:space-between; gap:14px; width:440px; max-width:100%; height:46px; padding:0 12px 0 10px; border-radius:999px; background:repeating-linear-gradient(0deg,rgba(255,255,255,0.014) 0px,rgba(255,255,255,0.014) 1px,rgba(0,0,0,0) 1px,rgba(0,0,0,0) 3px),linear-gradient(180deg,#353B46,#21252C); border:1px solid rgba(150,165,185,0.32); box-shadow:0 12px 28px rgba(0,0,0,0.4)">
        <div style="position:relative; flex:none; width:28px; height:28px; border-radius:50%; background:conic-gradient(from 200deg,#343A46,#6B7587,#434A58,#8693A6,#343A46)">
          <div style="position:absolute; inset:3px; border-radius:50%; background:#13161B"></div>
          <div style="position:absolute; left:7px; right:7px; top:13px; height:2px; border-radius:2px; background:linear-gradient(90deg,transparent,#8FA0B5,transparent)"></div>
        </div>
        <div style="font-family:'Michroma',sans-serif; font-size:10px; letter-spacing:0.18em; text-transform:uppercase; color:#98A4B5">Aura V4 · Shuttered</div>
        <button onclick="setShutter(false)" role="switch" aria-checked="false" aria-label="Wake Aura" style="position:relative; width:40px; height:22px; padding:0; border-radius:999px; border:1px solid rgba(0,0,0,0.55); background:linear-gradient(180deg,#14171C,#1B1F26)"><span style="position:absolute; top:2px; left:3px; width:16px; height:16px; border-radius:50%; background:linear-gradient(180deg,#6E6A60,#4A4840)"></span></button>
      </div>
      <div style="font-family:'JetBrains Mono',monospace; font-size:12px; color:#98A4B5; line-height:1.6">Ultra-minimal. Eye shuttered, voice and agents asleep.<br>Toggle wakes the island into compact.</div>
    </div>
  </div>

  <!-- Row 2: 03 Fully opened island -->
  <div id="expandedShell" style="display:flex; flex-direction:column; gap:12px">
    <div style="font-family:'Michroma',sans-serif; font-size:10px; letter-spacing:0.2em; text-transform:uppercase; color:#8FA0B5">03 · Fully opened island</div>
    <div style="position:relative; width:100%; padding:24px; border-radius:40px; background:repeating-linear-gradient(0deg,rgba(255,255,255,0.016) 0px,rgba(255,255,255,0.016) 1px,rgba(0,0,0,0) 1px,rgba(0,0,0,0) 3px),linear-gradient(115deg,rgba(225,236,255,0) 28%,rgba(225,236,255,0.07) 44%,rgba(225,236,255,0) 60%),linear-gradient(180deg,#444B57 0%,#2C2D33 38%,#21252C 100%); border:1px solid rgba(150,165,185,0.6); box-shadow:0 44px 100px rgba(0,0,0,0.6),0 0 0 1px rgba(0,0,0,0.6),inset 0 1px 0 rgba(215,228,250,0.25),inset 0 -2px 0 rgba(0,0,0,0.55)">
      <div style="position:absolute; top:-1px; left:50%; margin-left:-84px; width:168px; height:12px; clip-path:polygon(0 0,100% 0,90% 100%,10% 100%); background:linear-gradient(180deg,#DCE4EE,#5E6879)"></div>
      <div style="position:absolute; top:3px; left:50%; margin-left:-40px; width:80px; height:2px; border-radius:2px; background:#5CC3F2; box-shadow:0 0 10px rgba(70,170,230,0.9)"></div>
      <div style="position:relative; display:flex; align-items:center; gap:14px; height:76px; padding:0 16px 0 14px; border-radius:999px; background:linear-gradient(180deg,#171A20,#21252C); border:1px solid rgba(0,0,0,0.6)">
        <div style="position:relative; flex:none; width:54px; height:54px; border-radius:50%; background:conic-gradient(from 200deg,#4A5160,#C9D3E0,#6B7587,#E6EDF5,#4A5160,#98A5B8,#4A5160); box-shadow:0 3px 8px rgba(0,0,0,0.7),0 0 24px rgba(92,195,242,0.38)">
          <div style="position:absolute; inset:5px; border-radius:50%; background:#13161B"></div>
          <div style="position:absolute; inset:10px; border-radius:50%; border:1px solid rgba(92,195,242,0.65); background:radial-gradient(circle at 50% 50%,#EAF8FF 0%,#5CC3F2 11%,#0F3A5C 30%,#121316 62%)"></div>
          <div style="position:absolute; inset:17px; border-radius:50%; border:1px dashed rgba(92,195,242,0.35)"></div>
        </div>
        <div style="flex:none; width:1px; height:36px; background:rgba(0,0,0,0.7)"></div>
        <div style="position:relative; flex:none">
          <button id="modelBtn" onclick="toggleModelMenu()" class="pill-btn-steel" style="display:flex; align-items:center; gap:10px; height:42px; padding:0 16px; font-size:13px; font-weight:600">
            <span id="modelLabel">qwen3.5:4b · Ollama</span> ▾
          </button>
          <div id="modelMenu" class="dropdown-menu" style="position:absolute; z-index:5; top:50px; left:0; width:230px; padding:6px; border-radius:18px; background:linear-gradient(180deg,#464D5A,#2E343E); border:1px solid rgba(150,165,185,0.5); box-shadow:0 22px 44px rgba(0,0,0,0.55); flex-direction:column; gap:2px">
            <div class="model-opt active" onclick="pickModel('qwen3.5:4b','qwen3.5:4b · Ollama')">qwen3.5:4b · Ollama</div>
            <div class="model-opt" onclick="pickModel('qwen2.5-coder:7b','qwen2.5-coder:7b · Ollama')">qwen2.5-coder:7b · Ollama</div>
            <div class="model-opt" onclick="pickModel('cloud:gemini/gemini-3.8-flash','Gemini 3.8 · API')">Gemini 3.8 · API</div>
            <div class="model-opt" onclick="pickModel('cloud:custom/auto','FreeLLMAPI · Auto')">FreeLLMAPI · Auto</div>
          </div>
        </div>
        <div style="flex:none; width:1px; height:36px; background:rgba(0,0,0,0.7)"></div>
        <div style="flex:none; display:flex; align-items:center; gap:6px; flex-wrap:wrap">
          <button class="agent-chip active" onclick="pickAgentChip(this,'planner')">@planner</button>
          <button class="agent-chip" onclick="pickAgentChip(this,'coder')">@coder</button>
          <button class="agent-chip" onclick="pickAgentChip(this,'researcher')">@researcher</button>
          <button class="agent-chip" onclick="sendQuick('check my unread emails')">@gmail</button>
          <button class="agent-chip" onclick="sendQuick('what is on my calendar today?')">@calendar</button>
        </div>
        <div style="flex:1; min-width:140px; display:flex; flex-direction:column; align-items:center; gap:2px">
          <svg width="210" height="36" viewBox="0 0 210 36" aria-hidden="true">
            <defs>
              <linearGradient id="gfB" x1="0" x2="1" y1="0" y2="0"><stop offset="0" stop-color="#1F6FA8" stop-opacity="0"/><stop offset="0.25" stop-color="#1F6FA8"/><stop offset="0.5" stop-color="#D2EEFC"/><stop offset="0.75" stop-color="#1F6FA8"/><stop offset="1" stop-color="#1F6FA8" stop-opacity="0"/></linearGradient>
              <radialGradient id="rgB"><stop offset="0" stop-color="#5CC3F2" stop-opacity="0.9"/><stop offset="1" stop-color="#5CC3F2" stop-opacity="0"/></radialGradient>
            </defs>
            <path id="filHdrDim" d="" fill="none" stroke="url(#gfB)" stroke-width="1" opacity="0.45"/>
            <path id="filHdrMain" d="" fill="none" stroke="url(#gfB)" stroke-width="1.7" stroke-linecap="round"/>
            <circle cx="105" cy="18" r="8" fill="url(#rgB)"/>
            <circle cx="105" cy="18" r="2" fill="#E6F6FF"/>
          </svg>
          <div id="voiceLabel" style="font-family:'Michroma',sans-serif; font-size:9px; letter-spacing:0.2em; text-transform:uppercase; color:#8FA0B5">Ultron voice · ready</div>
        </div>
        <button id="islandAppsBtn" onclick="toggleAppsPanel()" class="pill-btn-steel" style="flex:none; height:38px; padding:0 14px; font-size:12px; font-weight:600">⊞ Apps</button>
        <button onclick="stopAura()" class="pill-btn-steel" style="flex:none; height:38px; padding:0 16px; font-size:13px; font-weight:600">■ Stop Aura</button>
        <button onclick="setShutter(true)" aria-label="Shutter" title="Shutter Island" class="pill-btn-steel" style="flex:none; width:40px; height:40px; padding:0; display:flex; align-items:center; justify-content:center">◐</button>
        <button id="islandShutdownBtn" onclick="shutdownAura()" aria-label="Shut Down AURA V4" title="Shut Down AURA V4 & Free VRAM" class="pill-btn-danger" style="flex:none; height:40px; padding:0 14px; font-size:13px; display:flex; align-items:center; gap:6px">⏻ Off</button>
      </div>

      <!-- Right-Corner Apps Connector Drawer -->
      <div id="appsPanel" style="display:none; margin-top:12px; padding:14px 16px; border-radius:20px; background:linear-gradient(180deg,#181C22,#12151A); border:1px solid rgba(150,165,185,0.35); gap:14px; flex-wrap:wrap; align-items:center">
        <div style="font-family:'Michroma',sans-serif; font-size:11px; letter-spacing:0.12em; text-transform:uppercase; color:#DCE4EE">Connect Apps</div>
        <label style="display:flex; align-items:center; gap:6px; font-size:12px; cursor:pointer">
          <input id="islGmailChk" type="checkbox" onchange="toggleIslandApp('gmail', this.checked)" style="accent-color:#6FCBF4"> Gmail (<span id="islGmailState">Off</span>)
        </label>
        <input id="islGmailAddr" type="email" placeholder="Gmail address" style="padding:6px 10px; border-radius:999px; border:1px solid rgba(150,165,185,0.28); background:#0E1014; color:#E8EEF6; font-size:12px; width:170px">
        <input id="islGmailPass" type="password" placeholder="App password" style="padding:6px 10px; border-radius:999px; border:1px solid rgba(150,165,185,0.28); background:#0E1014; color:#E8EEF6; font-size:12px; width:130px">
        <label style="display:flex; align-items:center; gap:6px; font-size:12px; cursor:pointer">
          <input id="islCalChk" type="checkbox" onchange="toggleIslandApp('calendar', this.checked)" style="accent-color:#6FCBF4"> Calendar (<span id="islCalState">Local</span>)
        </label>
        <input id="islCalUrl" type="url" placeholder="iCal feed URL (.ics)" style="padding:6px 10px; border-radius:999px; border:1px solid rgba(150,165,185,0.28); background:#0E1014; color:#E8EEF6; font-size:12px; width:180px">
        <button onclick="saveIslandApps()" class="pill-btn-arc" style="height:30px; padding:0 14px; font-size:12px">Save</button>
      </div>

      <!-- Body -->
      <div style="display:flex; gap:16px; margin-top:18px; min-height:476px">
        <!-- Left Chat Well -->
        <div style="flex:1; min-width:0; display:flex; flex-direction:column; gap:12px; padding:16px; border-radius:28px; background:linear-gradient(180deg,#14171C,#1A1E25); border:1px solid rgba(0,0,0,0.6)">
          <button onclick="toggleReasoning()" style="display:flex; align-items:center; gap:10px; width:100%; padding:10px 14px; border-radius:14px; border:1px solid rgba(150,165,185,0.3); background:linear-gradient(180deg,#353B46,#2B3039); color:#E8EDF4; font-size:13px; font-weight:500; text-align:left">
            ▾ Reasoning steps
            <span id="toolsBadge" style="margin-left:auto; padding:3px 11px; border-radius:999px; border:1px solid rgba(150,165,185,0.5); color:#8FD3F4; font-size:11px">0 turns</span>
          </button>
          <div id="reasoningTrace" style="display:none; padding:10px 14px; border-radius:12px; background:#101216; font-family:'JetBrains Mono',monospace; font-size:12px; color:#98A4B5">Awaiting execution metrics.</div>
          <div id="chatLog" class="chat-scroll">
            <div style="display:flex; gap:12px; align-items:flex-start">
              <div style="width:28px; height:28px; flex:none; border-radius:50%; border:1px solid #5CC3F2; background:radial-gradient(circle at 50% 50%,#EAF8FF 0%,#5CC3F2 18%,#0F3A5C 48%,#121316 80%)"></div>
              <div style="font-size:14px; line-height:1.55; color:#DCE3EC">Aura V4 Ultron online. Select an agent chip, ask naturally below, or trigger <span style="font-family:'JetBrains Mono',monospace; font-size:12px; padding:1px 6px; border-radius:6px; background:#2D333E; color:#8FD3F4">Summarize my day</span>.</div>
            </div>
          </div>
          <!-- Omni Composer -->
          <div style="display:flex; flex-direction:column; gap:10px; padding:12px 14px; border-radius:22px; background:linear-gradient(180deg,#2C313A,#353B46); border:1px solid rgba(150,165,185,0.5)">
            <input id="promptInput" type="text" placeholder="Ask Aura anything, or type / for commands and @ for agents" style="width:100%; border:none; outline:none; background:transparent; color:#E8EDF4; font-size:14px; font-family:inherit" onkeydown="if(event.key==='Enter') sendPrompt()">
            <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap">
              <button onclick="sendQuick('research online about ')" class="pill-btn-steel" style="height:28px; padding:0 12px; font-size:12px">DeepSearch</button>
              <button onclick="sendQuick('@planner analyze step-by-step: ')" class="pill-btn-steel" style="height:28px; padding:0 12px; font-size:12px">Think</button>
              <button onclick="sendQuick('what is on my screen?')" class="pill-btn-steel" style="height:28px; padding:0 12px; font-size:12px">Screen</button>
              <button onclick="sendQuick('git status')" class="pill-btn-steel" style="height:28px; padding:0 12px; font-size:12px">Git</button>
              <button onclick="sendPrompt()" class="pill-btn-arc" style="margin-left:auto; height:34px; padding:0 20px; font-size:13px">Send ↑</button>
            </div>
          </div>
        </div>

        <!-- Middle Column -->
        <div style="flex:none; width:280px; display:flex; flex-direction:column; gap:14px">
          <div style="display:flex; flex-direction:column; gap:12px; padding:16px; border-radius:24px; background:linear-gradient(180deg,rgba(70,170,230,0.2),rgba(70,170,230,0) 55%),linear-gradient(180deg,#3C4350,#292E37); border:1px solid rgba(150,165,185,0.4)">
            <div style="font-family:'Michroma',sans-serif; font-size:11px; letter-spacing:0.1em; text-transform:uppercase; color:#DCE4EE">◆ Executive summary</div>
            <button onclick="sendQuick('morning briefing')" class="pill-btn-arc" style="height:40px; font-size:14px">Summarize my day</button>
            <div style="font-size:11px; letter-spacing:0.16em; text-transform:uppercase; color:#98A4B5">Live briefing</div>
            <div id="briefingList" style="display:flex; flex-direction:column; gap:8px; font-size:13px; color:#DCE3EC">
              <div style="color:#98A4B5">Syncing workspace status...</div>
            </div>
          </div>
          <div style="flex:1; display:flex; flex-direction:column; gap:10px; padding:16px; border-radius:24px; background:linear-gradient(180deg,#353B46,#272C35); border:1px solid rgba(150,165,185,0.28)">
            <div style="display:flex; justify-content:space-between; align-items:center">
              <div style="font-family:'Michroma',sans-serif; font-size:11px; letter-spacing:0.1em; text-transform:uppercase; color:#DCE4EE">◆ Gmail</div>
              <button onclick="sendQuick('check my unread emails')" class="pill-btn-steel" style="height:24px; padding:0 10px; font-size:11px">Inbox</button>
            </div>
            <div id="gmailList" style="display:flex; flex-direction:column; gap:8px">
              <div style="font-size:12px; color:#98A4B5">Gmail not connected — open Apps to connect Gmail.</div>
            </div>
          </div>
        </div>

        <!-- Right Column -->
        <div style="flex:none; width:280px; display:flex; flex-direction:column; gap:14px">
          <div style="display:flex; flex-direction:column; gap:10px; padding:16px; border-radius:24px; background:linear-gradient(180deg,#353B46,#272C35); border:1px solid rgba(150,165,185,0.28)">
            <div style="display:flex; justify-content:space-between; align-items:center">
              <div style="font-family:'Michroma',sans-serif; font-size:11px; letter-spacing:0.1em; text-transform:uppercase; color:#DCE4EE">◆ Agenda</div>
              <div id="agendaDate" style="font-size:12px; color:#98A4B5"></div>
            </div>
            <div id="agendaList" style="display:flex; flex-direction:column; gap:8px">
              <div style="font-size:12px; color:#98A4B5">No events scheduled today.</div>
            </div>
          </div>
          <div style="flex:1; display:flex; flex-direction:column; gap:10px; padding:16px; border-radius:24px; background:linear-gradient(180deg,#353B46,#272C35); border:1px solid rgba(150,165,185,0.28)">
            <div style="display:flex; justify-content:space-between; align-items:center">
              <div style="font-family:'Michroma',sans-serif; font-size:11px; letter-spacing:0.1em; text-transform:uppercase; color:#DCE4EE">◆ Todos</div>
              <div id="todoCount" style="font-size:12px; color:#98A4B5">0 of 0 done</div>
            </div>
            <div style="height:4px; border-radius:999px; background:#14171C; overflow:hidden"><div id="todoBar" style="width:0%; height:100%; background:linear-gradient(90deg,#1F6FA8,#7AD0F5)"></div></div>
            <div id="todoList" style="display:flex; flex-direction:column; gap:6px">
              <div style="font-size:12px; color:#98A4B5">No to-dos added yet.</div>
            </div>
            <div style="display:flex; gap:6px; margin-top:auto">
              <input id="todoAddInput" type="text" placeholder="Add to-do..." style="flex:1; min-width:0; padding:6px 10px; border-radius:999px; border:1px solid rgba(150,165,185,0.28); background:#14171C; color:#E8EEF6; font-size:12px; outline:none" onkeydown="if(event.key==='Enter')addTodo()">
              <button onclick="addTodo()" class="pill-btn-steel" style="height:28px; padding:0 12px; font-size:12px">+</button>
            </div>
          </div>
        </div>
      </div>

      <!-- Bottom bar -->
      <div style="display:flex; align-items:center; justify-content:space-between; margin-top:16px; flex-wrap:wrap; gap:12px">
        <div style="display:flex; align-items:center; gap:8px">
          <button class="tab-pill active">Ask Aura</button>
          <button class="tab-pill" onclick="location.href='/'">Full Web UI</button>
        </div>
        <div id="statusMeta" style="font-family:'JetBrains Mono',monospace; font-size:12px; color:#98A4B5">Ready · Enter to send</div>
      </div>
    </div>
  </div>
</div>

<script>
  let selectedRole = 'planner', turns = 0, phase = 0, busy = false;
  document.getElementById('agendaDate').textContent = new Date().toLocaleDateString('en-GB', { weekday:'short', day:'numeric', month:'short' });

  function wavePath(w, h, amp, ph, mult) {
    const mid = h / 2; let d = '';
    for (let i = 0; i <= 40; i++) {
      const u = i / 40, x = (u * w).toFixed(1), env = Math.exp(-Math.pow((u - 0.5) / 0.24, 2));
      const y = (mid - amp * env * (0.7 * Math.sin((u * 6.5 - ph * mult) * Math.PI) + 0.3 * Math.cos((u * 13 + ph * 1.4) * Math.PI))).toFixed(1);
      d += (i === 0 ? 'M' : 'L') + x + ',' + y + ' ';
    }
    return d;
  }
  function anim() {
    phase += busy ? 0.16 : 0.05;
    const amp = busy ? 11.5 : 5.5;
    document.getElementById('filCompactMain').setAttribute('d', wavePath(150, 36, amp, phase, 0.9));
    document.getElementById('filCompactDim').setAttribute('d', wavePath(150, 36, amp * 0.6, -phase, 0.6));
    document.getElementById('filHdrMain').setAttribute('d', wavePath(210, 36, amp, phase, 0.9));
    document.getElementById('filHdrDim').setAttribute('d', wavePath(210, 36, amp * 0.6, -phase, 0.6));
    requestAnimationFrame(anim);
  }
  anim();

  function setShutter(on) {
    document.getElementById('expandedShell').style.opacity = on ? '0.28' : '1';
    document.getElementById('voiceLabel').textContent = on ? 'Ultron · Shuttered' : 'Ultron voice · ready';
  }
  function toggleModelMenu() { document.getElementById('modelMenu').classList.toggle('open'); }
  async function pickModel(spec, label) {
    document.getElementById('modelMenu').classList.remove('open');
    document.getElementById('modelLabel').textContent = label;
    document.getElementById('compactModelBtn').textContent = spec.split('/').pop();
    await fetch('/api/model', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ low_vram:false, model_override: spec }) });
  }
  function pickAgentChip(btn, role) {
    selectedRole = role;
    document.querySelectorAll('.agent-chip').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }
  function toggleReasoning() {
    const el = document.getElementById('reasoningTrace');
    el.style.display = el.style.display === 'none' ? 'block' : 'none';
  }
  function renderDashboard(snap) {
    if (!snap || typeof snap !== 'object') return;
    const bList = document.getElementById('briefingList');
    if (bList && Array.isArray(snap.briefing)) {
      bList.textContent = '';
      (snap.briefing.length ? snap.briefing : ['No workspace summary yet.']).forEach(line => {
        const d = document.createElement('div');
        d.textContent = '◆ ' + line;
        bList.appendChild(d);
      });
    }
    const gList = document.getElementById('gmailList');
    if (gList && Array.isArray(snap.gmail)) {
      gList.textContent = '';
      if (!snap.gmail.length) {
        const d = document.createElement('div');
        d.style.cssText = 'font-size:12px;color:#98A4B5';
        d.textContent = 'Gmail not connected — open Apps to link Gmail.';
        gList.appendChild(d);
      } else {
        snap.gmail.forEach(m => {
          const box = document.createElement('div');
          box.style.cssText = 'display:flex;flex-direction:column;gap:4px;padding:10px 12px;border-radius:14px;background:linear-gradient(180deg,#14171C,#1B1F26)';
          const hdr = document.createElement('div');
          hdr.style.cssText = 'display:flex;justify-content:space-between;font-size:13px;font-weight:500';
          const sp1 = document.createElement('span'); sp1.textContent = m.sender || 'Gmail';
          const sp2 = document.createElement('span'); sp2.style.cssText = 'font-size:11px;color:#98A4B5'; sp2.textContent = m.time || '';
          hdr.appendChild(sp1); hdr.appendChild(sp2);
          const sub = document.createElement('div'); sub.style.cssText = 'font-size:12px;color:#98A4B5'; sub.textContent = m.subject || '';
          box.appendChild(hdr); box.appendChild(sub);
          gList.appendChild(box);
        });
      }
    }
    const aList = document.getElementById('agendaList');
    if (aList && Array.isArray(snap.agenda)) {
      aList.textContent = '';
      if (!snap.agenda.length) {
        const d = document.createElement('div');
        d.style.cssText = 'font-size:12px;color:#98A4B5';
        d.textContent = 'No events scheduled today.';
        aList.appendChild(d);
      } else {
        snap.agenda.forEach(ev => {
          const row = document.createElement('div');
          row.style.cssText = 'display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:14px;background:linear-gradient(180deg,#14171C,#1B1F26)';
          const tm = document.createElement('div'); tm.style.cssText = 'width:68px;font-size:11px;color:#6FCBF4;line-height:1.25'; tm.textContent = ev.start_time || 'Today';
          const tt = document.createElement('div'); tt.style.cssText = 'flex:1;font-size:13px;font-weight:500'; tt.textContent = ev.title || '';
          row.appendChild(tm); row.appendChild(tt);
          aList.appendChild(row);
        });
      }
    }
    const tList = document.getElementById('todoList');
    if (tList && Array.isArray(snap.todos)) {
      tList.textContent = '';
      const total = snap.todos.length;
      const done = snap.todos.filter(t => t.done).length;
      document.getElementById('todoCount').textContent = `${done} of ${total} done`;
      document.getElementById('todoBar').style.width = total ? `${Math.round((done / total) * 100)}%` : '0%';
      if (!total) {
        const d = document.createElement('div');
        d.style.cssText = 'font-size:12px;color:#98A4B5';
        d.textContent = 'No to-dos added yet.';
        tList.appendChild(d);
      } else {
        snap.todos.forEach(item => {
          const lbl = document.createElement('label');
          lbl.style.cssText = 'display:flex;align-items:center;gap:10px;font-size:13px;cursor:pointer';
          const chk = document.createElement('input');
          chk.type = 'checkbox';
          chk.className = 'todo-chk';
          chk.checked = !!item.done;
          chk.style.cssText = 'width:16px;height:16px;accent-color:#6FCBF4';
          chk.onchange = () => toggleTodo(item.text, chk.checked);
          lbl.appendChild(chk);
          lbl.appendChild(document.createTextNode(item.text || ''));
          tList.appendChild(lbl);
        });
      }
    }
  }
  async function loadDashboard() {
    try {
      const snap = await (await fetch('/api/dashboard-data')).json();
      renderDashboard(snap);
      if (snap && snap.apps) syncIslandApps(snap.apps);
    } catch (_) {}
  }
  async function addTodo() {
    const inp = document.getElementById('todoAddInput');
    const txt = (inp && inp.value || '').trim();
    if (!txt) return;
    inp.value = '';
    try {
      const snap = await (await fetch('/api/todos', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ action:'add', text: txt }) })).json();
      renderDashboard(snap);
    } catch (_) {}
  }
  async function toggleTodo(text, done) {
    try {
      const snap = await (await fetch('/api/todos', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ action:'toggle', text, done }) })).json();
      renderDashboard(snap);
    } catch (_) {}
  }
  function toggleAppsPanel() {
    const p = document.getElementById('appsPanel');
    if (p) p.style.display = p.style.display === 'none' ? 'flex' : 'none';
  }
  function syncIslandApps(apps) {
    if (!apps) return;
    const g = apps.gmail || {}, c = apps.calendar || {};
    const gc = document.getElementById('islGmailChk'); if (gc) gc.checked = !!g.enabled;
    const cc = document.getElementById('islCalChk'); if (cc) cc.checked = !!c.enabled;
    const gs = document.getElementById('islGmailState'); if (gs) gs.textContent = g.connected ? (g.account || 'On') : 'Off';
    const cs = document.getElementById('islCalState'); if (cs) cs.textContent = c.connected ? (c.source || 'On') : 'Local';
    const btn = document.getElementById('islandAppsBtn');
    const n = (g.connected ? 1 : 0) + (c.connected ? 1 : 0);
    if (btn) btn.textContent = n ? `⊞ Apps · ${n}` : '⊞ Apps';
  }
  async function toggleIslandApp(kind, enabled) {
    const body = kind === 'gmail' ? { gmail_enabled: !!enabled } : { calendar_enabled: !!enabled };
    try {
      const data = await (await fetch('/api/apps/connect', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) })).json();
      if (data.apps) syncIslandApps(data.apps);
      if (data.dashboard) renderDashboard(data.dashboard);
    } catch (_) {}
  }
  async function saveIslandApps() {
    const payload = {
      gmail_enabled: !!document.getElementById('islGmailChk').checked,
      calendar_enabled: !!document.getElementById('islCalChk').checked,
    };
    const ga = document.getElementById('islGmailAddr').value.trim();
    const gp = document.getElementById('islGmailPass').value.trim();
    const cu = document.getElementById('islCalUrl').value.trim();
    if (ga) payload.gmail_address = ga;
    if (gp) payload.gmail_app_password = gp;
    if (cu) payload.gcal_ical_url = cu;
    try {
      const data = await (await fetch('/api/apps/connect', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) })).json();
      if (data.apps) syncIslandApps(data.apps);
      if (data.dashboard) renderDashboard(data.dashboard);
      document.getElementById('islGmailPass').value = '';
    } catch (_) {}
  }
  function addBubble(who, text) {
    const log = document.getElementById('chatLog');
    const div = document.createElement('div');
    if (who === 'user') {
      div.style.cssText = 'align-self:flex-end;max-width:78%;padding:10px 15px;border-radius:18px 18px 6px 18px;background:linear-gradient(180deg,#3C4350,#2E343E);border:1px solid rgba(150,165,185,0.3);font-size:14px';
    } else {
      div.style.cssText = 'padding:10px 14px;border-radius:14px;background:rgba(255,255,255,0.03);font-size:14px;line-height:1.55;white-space:pre-wrap';
    }
    div.textContent = text;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }
  function sendQuick(p) {
    const inp = document.getElementById('promptInput');
    inp.value = p;
    if (!p.endsWith(' ')) sendPrompt();
    else inp.focus();
  }
  async function sendPrompt() {
    const inp = document.getElementById('promptInput');
    const prompt = inp.value.trim();
    if (!prompt || busy) return;
    inp.value = '';
    busy = true;
    addBubble('user', prompt);
    document.getElementById('voiceLabel').textContent = 'Ultron · Thinking';
    try {
      const res = await fetch('/api/chat', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ prompt, agent_role: selectedRole }) });
      const data = await res.json();
      turns += 1;
      document.getElementById('toolsBadge').textContent = `${turns} turn${turns === 1 ? '' : 's'}`;
      document.getElementById('reasoningTrace').textContent = data.metrics || 'Completed';
      addBubble('ai', data.answer || '(done)');
    } catch (e) {
      addBubble('ai', 'Error: ' + e);
    } finally {
      busy = false;
      document.getElementById('voiceLabel').textContent = 'Ultron voice · ready';
      loadDashboard();
    }
  }
  async function stopAura() {
    await fetch('/api/stop', { method:'POST' });
    busy = false;
    document.getElementById('voiceLabel').textContent = 'Ultron · stopped';
  }
  async function shutdownAura() {
    const lbl = document.getElementById('voiceLabel');
    if (lbl) lbl.textContent = 'AURA · freeing VRAM & shutting down...';
    const btn = document.getElementById('islandShutdownBtn');
    const cbtn = document.getElementById('compactShutdownBtn');
    if (btn) btn.disabled = true;
    if (cbtn) cbtn.disabled = true;
    try {
      const res = await fetch('/api/shutdown', { method:'POST' });
      const data = await res.json();
      const freed = (data.unloaded || []).length;
      if (lbl) lbl.textContent = freed ? `AURA off · freed ${freed} model(s)` : 'AURA V4 shut down';
      addBubble('ai', 'AURA V4 shut down cleanly and models unloaded from VRAM.');
    } catch (_) {
      if (lbl) lbl.textContent = 'AURA V4 shut down';
    }
  }
  (async () => {
    try {
      const st = await (await fetch('/api/status')).json();
      const mod = st.model_override || (st.roles && st.roles.lightweight) || 'qwen3.5:4b';
      document.getElementById('modelLabel').textContent = `${mod} · Ollama`;
      document.getElementById('compactModelBtn').textContent = mod;
      document.getElementById('statusMeta').textContent = `${st.read_only ? 'RO' : 'RW'} · ${st.tools_count || 0} MCP tools · Enter to send`;
    } catch (_) {}
    loadDashboard();
  })();
</script>
</body>
</html>
"""
