"""evaluator.py - Issue triage and feasibility evaluator for human and agent gates."""

from __future__ import annotations

import logging
import re
from typing import Literal

from issue_agent.models import Issue, TriageDecision

logger = logging.getLogger("issue_agent.evaluator")

# Known architectural components in ai-potluck
KNOWN_COMPONENTS = [
    "cli",
    "wire",
    "canonical",
    "records",
    "identity",
    "metering",
    "allocation",
    "ledger",
    "settlement",
    "reproducible",
    "transparency",
    "verify",
    "reconcile",
    "statemachine",
    "coordinator",
    "connector",
    "adapters",
    "websocket",
    "proxy",
    "tls",
    "persistence",
    "deploy",
    "errors",
    "ux",
    "docs",
    "tests",
]

# Invariants that require strict human oversight (cannot be auto-approved by agent alone)
STRICT_INVARIANT_KEYWORDS = [
    "domain separator",
    "rfc 8785",
    "ed25519",
    "breaking signing",
    "signing semantics",
    "hard fork",
    "delete all data",
    "drop table",
]

HUMAN_APPROVAL_LABELS = {
    "bot:approved",
    "agent:approved",
    "ready-to-fix",
    "approved",
    "auto-fix",
    "triage:ready",
}

HUMAN_APPROVAL_COMMANDS = {
    "/fix",
    "/approve",
    "/start",
    "approve",
}


class IssueEvaluator:
    """Evaluates whether an issue is actionable, human-approved, or agent-feasible."""

    def __init__(
        self,
        auto_approve_threshold: float = 0.70,
        known_components: list[str] | None = None,
    ) -> None:
        self.auto_approve_threshold = auto_approve_threshold
        self.known_components = known_components or KNOWN_COMPONENTS

    def evaluate(
        self,
        issue: Issue,
        force_human_approve: bool = False,
        comments: list[str] | None = None,
    ) -> TriageDecision:
        """Evaluate an issue against human approval gates and agent feasibility criteria."""
        # 1. Check for explicit human approval via CLI argument
        if force_human_approve:
            logger.info("Issue #%s explicitly approved by operator flag.", issue.id)
            return TriageDecision(
                approved=True,
                source="human",
                confidence=1.0,
                reason="Explicitly approved by operator CLI flag",
                complexity="medium",
                target_components=self._detect_components(issue),
                suggested_approach="Execute user-confirmed repair workflow.",
            )

        # 2. Check for human approval via issue labels
        for lbl in issue.labels:
            if lbl.lower().strip() in HUMAN_APPROVAL_LABELS:
                logger.info("Issue #%s has human approval label '%s'.", issue.id, lbl)
                return TriageDecision(
                    approved=True,
                    source="human",
                    confidence=1.0,
                    reason=f"Approved by human operator via issue label '{lbl}'",
                    complexity="medium",
                    target_components=self._detect_components(issue),
                    suggested_approach="Proceed with resolution authorized by issue label.",
                )

        # 3. Check for human approval in issue comments (e.g. "/fix")
        if comments:
            for comment in comments:
                clean_comment = comment.strip().lower()
                for cmd in HUMAN_APPROVAL_COMMANDS:
                    if cmd in clean_comment:
                        logger.info("Issue #%s approved by comment command '%s'.", issue.id, cmd)
                        return TriageDecision(
                            approved=True,
                            source="human",
                            confidence=1.0,
                            reason=f"Approved by operator via comment command '{cmd}'",
                            complexity="medium",
                            target_components=self._detect_components(issue),
                            suggested_approach="Proceed with resolution triggered by comment.",
                        )

        # 4. Agent Autonomous Evaluation
        return self._evaluate_by_agent(issue)

    def _evaluate_by_agent(self, issue: Issue) -> TriageDecision:
        """Agent performs autonomous self-evaluation of feasibility, safety, and scope."""
        full_text = f"{issue.title}\n{issue.body}".lower()

        # Check safety invariants first: strict trust-root rules
        for keyword in STRICT_INVARIANT_KEYWORDS:
            if keyword in full_text:
                return TriageDecision(
                    approved=False,
                    source="agent",
                    confidence=0.20,
                    reason=(
                        f"Detected strict trust-root invariant '{keyword}'. "
                        "AGENTS.md mandates explicit human review for cryptographic changes."
                    ),
                    complexity="high",
                    target_components=["records", "canonical"],
                    suggested_approach="Flag for human cryptographic architecture review.",
                )

        # Detect components
        detected_components = self._detect_components(issue)

        # Score calculation factors
        score = 0.0
        reasons: list[str] = []

        # Factor A: Component specificity
        if detected_components:
            score += 0.30
            reasons.append(f"Identified component(s): {', '.join(detected_components)}")
        else:
            score += 0.05
            reasons.append("General / unclassified component")

        # Factor B: Error trace or code snippet presence
        has_trace = bool(re.search(r"traceback|error:|exception|def |class |```", full_text))
        if has_trace:
            score += 0.25
            reasons.append("Contains traceback or code block")

        # Factor C: Clear expected vs actual or bug symptoms
        has_symptoms = bool(
            re.search(r"expected|instead|repro|fails|broken|regression|fix|bug", full_text)
        )
        if has_symptoms:
            score += 0.25
            reasons.append("Clear problem description / symptoms")

        # Factor D: Conciseness and actionable length
        body_words = len(issue.body.split())
        if 10 <= body_words <= 500:
            score += 0.15
            reasons.append(f"Well-scoped issue body ({body_words} words)")
        elif body_words < 10:
            score -= 0.20
            reasons.append("Issue description too brief / underspecified")

        # Factor E: Title clarity
        if len(issue.title.strip()) >= 8:
            score += 0.05

        score = max(0.0, min(1.0, score))

        # Determine complexity
        complexity: Literal["low", "medium", "high"]
        if len(detected_components) > 3 or "coordinator" in detected_components:
            complexity = "high"
        elif any(c in detected_components for c in ("cli", "docs", "tests")):
            complexity = "low"
        else:
            complexity = "medium"

        # Auto-approval gate
        approved = (score >= self.auto_approve_threshold) and (complexity != "high")
        first_comp = detected_components[0] if detected_components else "target module"
        approach = (
            f"Reproduce issue in {first_comp}, implement minimal fix, "
            "add regression tests, verify with ruff/mypy/pytest."
        )

        return TriageDecision(
            approved=approved,
            source="agent",
            confidence=score,
            reason="; ".join(reasons),
            complexity=complexity,
            target_components=detected_components,
            suggested_approach=approach,
        )

    def _detect_components(self, issue: Issue) -> list[str]:
        text = f"{issue.title} {issue.body}".lower()
        found: list[str] = []
        for c in self.known_components:
            if re.search(rf"\b{re.escape(c)}\b", text):
                found.append(c)
        return found
