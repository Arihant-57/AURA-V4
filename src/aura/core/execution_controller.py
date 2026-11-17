"""
Progress-aware execution controller for AURA V4.

Replaces the blind step counter with a controller that tracks
useful vs wasted work and can extend budget when making progress
or terminate early when stuck.
"""

from __future__ import annotations

import hashlib as _hashlib
import json as _json
import logging
from collections import deque as _deque
from dataclasses import dataclass, field
from typing import Any as _Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoopGuardConfig:
    """Configuration for deterministic tool-loop prevention and context compaction."""

    max_identical_calls: int = 3
    ping_pong_window: int = 8
    default_poll_budget: int = 4
    compress_after_messages: int = 18
    preserve_recent_messages: int = 8
    max_observation_chars: int = 900


@dataclass(frozen=True)
class LoopVerdict:
    """Decision returned by LoopGuard before executing a tool call."""

    action: str  # "allow" | "warn" | "block"
    reason: str = ""
    call_count: int = 1


class LoopGuard:
    """Tracks tool call signatures to block identical loops and ping-pong cycles."""

    def __init__(self, config: LoopGuardConfig | None = None) -> None:
        self.config = config or LoopGuardConfig()
        self._call_counts: dict[str, int] = {}
        self._poll_counts: dict[str, int] = {}
        self._recent_sigs: _deque[str] = _deque(maxlen=max(6, self.config.ping_pong_window))

    @staticmethod
    def _hash_call(tool_name: str, args: dict[str, _Any] | str | None) -> str:
        try:
            canonical = _json.dumps(args or {}, sort_keys=True, default=str)
        except Exception:
            canonical = str(args or "")
        payload = f"{tool_name}:{canonical}".encode("utf-8", errors="replace")
        return _hashlib.sha256(payload).hexdigest()[:16]

    def _detect_ping_pong(self) -> int | None:
        sigs = list(self._recent_sigs)
        for period in (2, 3):
            if len(sigs) >= period * 2 and sigs[-period * 2:-period] == sigs[-period:]:
                return period
        return None

    def check_call(
        self,
        tool_name: str,
        args: dict[str, _Any] | None = None,
        *,
        is_poll: bool = False,
    ) -> LoopVerdict:
        """Evaluate whether a tool call should be allowed, warned, or hard-blocked."""
        sig = self._hash_call(tool_name, args)
        count = self._call_counts.get(sig, 0) + 1
        self._call_counts[sig] = count
        self._recent_sigs.append(sig)

        if count >= max(2, self.config.max_identical_calls):
            return LoopVerdict(
                action="block",
                reason=(
                    f"LoopGuard blocked repeated identical call to '{tool_name}' "
                    f"({count}x with same arguments). Try a different approach or summarize findings."
                ),
                call_count=count,
            )

        period = self._detect_ping_pong()
        if period is not None:
            return LoopVerdict(
                action="block",
                reason=(
                    f"LoopGuard detected a {period}-step ping-pong tool cycle ending at '{tool_name}'. "
                    "Stop repeating the cycle and pivot or answer now."
                ),
                call_count=count,
            )

        if is_poll:
            polls = self._poll_counts.get(tool_name, 0) + 1
            self._poll_counts[tool_name] = polls
            if polls > max(1, self.config.default_poll_budget):
                return LoopVerdict(
                    action="block",
                    reason=(
                        f"LoopGuard poll budget exceeded for '{tool_name}' "
                        f"({polls}/{self.config.default_poll_budget})."
                    ),
                    call_count=count,
                )

        if count >= max(2, self.config.max_identical_calls - 1):
            return LoopVerdict(
                action="warn",
                reason=(
                    f"'{tool_name}' was already called with identical arguments; "
                    "next identical attempt will be blocked."
                ),
                call_count=count,
            )
        return LoopVerdict(action="allow", reason="", call_count=count)

    def compress_context(
        self,
        messages: list[dict[str, _Any]],
        *,
        max_chars: int = 24000,
    ) -> list[dict[str, _Any]]:
        """4-stage context compaction preserving system prompt, initial task, and recent tail."""
        keep_tail = max(2, self.config.preserve_recent_messages)
        if len(messages) <= max(self.config.compress_after_messages, keep_tail + 2):
            total_chars = sum(len(str(m.get("content") or "")) for m in messages)
            if total_chars <= max_chars:
                return list(messages)

        head_end = 2 if len(messages) >= 2 and messages[0].get("role") == "system" else 1
        tail_start = max(head_end, len(messages) - keep_tail)
        head = [dict(m) for m in messages[:head_end]]
        middle = [dict(m) for m in messages[head_end:tail_start]]
        tail = [dict(m) for m in messages[tail_start:]]
        if not middle:
            return head + tail

        seen_obs: set[str] = set()
        obs_limit = max(300, self.config.max_observation_chars)
        compacted_mid: list[dict[str, _Any]] = []
        for msg in middle:
            content = str(msg.get("content") or "")
            role = str(msg.get("role") or "")
            if role == "tool" and content:
                digest = _hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]
                if digest in seen_obs:
                    msg["content"] = "[earlier identical tool output omitted by LoopGuard]"
                else:
                    seen_obs.add(digest)
                    if len(content) > obs_limit:
                        half = obs_limit // 2
                        msg["content"] = (
                            content[:half]
                            + f"\n... [{len(content) - obs_limit} chars omitted] ...\n"
                            + content[-half:]
                        )
            elif len(content) > obs_limit * 2:
                half = obs_limit
                msg["content"] = (
                    content[:half]
                    + f"\n... [{len(content) - (obs_limit * 2)} chars omitted] ...\n"
                    + content[-half:]
                )
            compacted_mid.append(msg)

        combined = head + compacted_mid + tail
        total_chars = sum(len(str(m.get("content") or "")) for m in combined)
        if total_chars <= max_chars and len(combined) <= max(self.config.compress_after_messages + 4, keep_tail + 4):
            return combined

        tool_names: list[str] = []
        for msg in compacted_mid:
            tname = msg.get("tool_name") or msg.get("name")
            if tname and tname not in tool_names:
                tool_names.append(str(tname))
            for tc in msg.get("tool_calls") or []:
                fn = (tc.get("function") or {}).get("name") if isinstance(tc, dict) else None
                if fn and fn not in tool_names:
                    tool_names.append(str(fn))
        tools_note = f" (tools used: {', '.join(tool_names[:8])})" if tool_names else ""
        summary_msg = {
            "role": "assistant",
            "content": f"[LoopGuard compacted {len(compacted_mid)} intermediate messages{tools_note}.]",
        }
        return head + [summary_msg] + tail


@dataclass
class ExecutionController:
    """Tracks agent progress and decides whether to continue or stop.

    Integrates with the agent loop. The loop calls ``tick()`` after each
    tool-call turn and checks ``should_continue`` before the next step.
    """

    max_steps: int = 10
    extension_budget: int = 5  # extra steps granted when making progress
    min_progress_ratio: float = 0.3  # at least 30% useful to grant extension

    # ── internal state ──────────────────────────────────────────────────
    step: int = field(default=0, init=False)
    useful_steps: int = field(default=0, init=False)
    failed_steps: int = field(default=0, init=False)
    repeated_steps: int = field(default=0, init=False)
    consecutive_failures: int = field(default=0, init=False)
    consecutive_repeats: int = field(default=0, init=False)
    _extended: bool = field(default=False, init=False)

    def tick(self, *, useful: bool, repeated: bool = False, failed: bool = False) -> None:
        """Record the outcome of one tool-call turn."""
        self.step += 1

        if repeated:
            self.repeated_steps += 1
            self.consecutive_repeats += 1
            self.consecutive_failures = 0
        elif failed:
            self.failed_steps += 1
            self.consecutive_failures += 1
            self.consecutive_repeats = 0
        elif useful:
            self.useful_steps += 1
            self.consecutive_failures = 0
            self.consecutive_repeats = 0

    @property
    def progress_ratio(self) -> float:
        if self.step == 0:
            return 1.0
        return self.useful_steps / self.step

    @property
    def budget_remaining(self) -> int:
        limit = self.max_steps + (self.extension_budget if self._extended else 0)
        return max(0, limit - self.step)

    @property
    def should_continue(self) -> bool:
        """Whether the loop should attempt another step."""
        # Hard stop: consecutive failures
        if self.consecutive_failures >= 3:
            logger.info("Execution stopped: 3 consecutive failures.")
            return False

        # Hard stop: stuck repeating
        if self.consecutive_repeats >= 3:
            logger.info("Execution stopped: 3 consecutive repeated calls.")
            return False

        # Within base budget: always continue
        if self.step < self.max_steps:
            return True

        # Beyond base budget: check if extension is warranted
        if not self._extended and self.progress_ratio >= self.min_progress_ratio:
            self._extended = True
            logger.info(
                "Granting %d extension steps (progress ratio: %.1f%%).",
                self.extension_budget, self.progress_ratio * 100,
            )

        limit = self.max_steps + (self.extension_budget if self._extended else 0)
        return self.step < limit

    @property
    def stop_reason(self) -> str:
        if self.consecutive_failures >= 3:
            return "consecutive_failures"
        if self.consecutive_repeats >= 3:
            return "consecutive_repeats"
        if self.budget_remaining <= 0:
            return "budget_exhausted"
        return ""

    def summary(self) -> str:
        return (
            f"Steps: {self.step}/{self.max_steps}"
            f"{'+' + str(self.extension_budget) if self._extended else ''}, "
            f"useful={self.useful_steps}, failed={self.failed_steps}, "
            f"repeated={self.repeated_steps}, "
            f"progress={self.progress_ratio:.0%}"
        )
