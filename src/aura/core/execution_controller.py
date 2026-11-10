"""
Progress-aware execution controller for AURA V4.

Replaces the blind step counter with a controller that tracks
useful vs wasted work and can extend budget when making progress
or terminate early when stuck.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


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
