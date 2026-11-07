"""
Diagnosis and Flow Validators for AURA V4.

Ensures LLM synthesis responses adhere to code semantics and
diagnosis reporting standards.
"""

from __future__ import annotations

import re
from typing import Optional


def _redesign_violations(answer: str) -> list[str]:
    """Detect if a diagnosis proposes changing established design intent."""
    violations = []
    # Simplified patterns for V4 foundation
    if re.search(r"remove\s+the\s+state\s+flag", answer, re.IGNORECASE):
        violations.append("Removing a state flag without proof of defect is a design change.")
    return violations


def validate_diagnosis_answer(answer: str, diagnosis_active: bool) -> Optional[str]:
    """Validate diagnosis format and design-change labeling."""
    if not diagnosis_active:
        return None

    sections = [
        "OBSERVED BEHAVIOR", "CONFIRMED DEFECT",
        "BEHAVIOR OF UNKNOWN INTENT", "MINIMAL FIX", "INVARIANT CHECK"
    ]
    missing = [s for s in sections if s not in answer.upper()]
    if missing:
        return f"Missing required diagnosis sections: {', '.join(missing)}"

    violations = _redesign_violations(answer)
    if violations and "DESIGN CHANGE" not in answer.upper():
        return violations[0]

    return None


def validate_control_flow(answer: str, evidence_text: str) -> list[str]:
    """Validate answer against fundamental control-flow semantics."""
    violations = []

    # Semantic Check 1: Deep merge hallucination
    if "merge" in answer.lower() and "deep" in answer.lower():
        if "spread" in evidence_text or "{..." in evidence_text or re.search(r"\{\s*\.\.\.", evidence_text):
            if "deep-merge" not in evidence_text.lower():
                violations.append("JavaScript object spread is a shallow merge, not deep.")

    # Semantic Check 2: Unconditional success across await rejection
    if "always returns" in answer.lower() or "unconditionally" in answer.lower():
        if "await" in evidence_text.lower():
            violations.append("Do not claim unconditional success when an awaited operation can fail.")

    return violations
