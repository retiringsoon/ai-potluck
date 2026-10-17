"""models.py - Domain data models for the Issue Resolver Agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class ChannelType(StrEnum):
    """Supported or reserved issue intake channels."""

    GITHUB = "github"
    DISCORD = "discord"
    TELEGRAM = "telegram"
    FEISHU = "feishu"
    LOCAL = "local"


class IssueStatus(StrEnum):
    """Status lifecycle for an issue handled by the agent."""

    OPEN = "open"
    TRIAGED = "triaged"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"
    REJECTED = "rejected"


@dataclass
class Issue:
    """Standardized issue representation across all intake channels."""

    id: str
    channel: ChannelType
    title: str
    body: str
    author: str
    url: str
    labels: list[str] = field(default_factory=list)
    created_at: str = ""
    raw_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def full_key(self) -> str:
        return f"{self.channel.value}#{self.id}"


@dataclass
class TriageDecision:
    """Outcome of evaluating whether an issue can be acted upon."""

    approved: bool
    source: Literal["human", "agent"]
    confidence: float  # Range: 0.0 to 1.0
    reason: str
    complexity: Literal["low", "medium", "high"] = "medium"
    target_components: list[str] = field(default_factory=list)
    suggested_approach: str = ""

    def summary(self) -> str:
        tag = "APPROVED" if self.approved else "NEEDS_REVIEW"
        return (
            f"[{tag}] by {self.source.upper()} (confidence: {self.confidence:.2f}, "
            f"complexity: {self.complexity})\nReason: {self.reason}"
        )


@dataclass
class VerificationResult:
    """Detailed evidence-based verification metrics."""

    passed: bool
    ruff_passed: bool
    mypy_passed: bool
    pytest_passed: bool
    ruff_output: str = ""
    mypy_output: str = ""
    pytest_output: str = ""
    summary: str = ""


@dataclass
class PipelineResult:
    """Final outcome of executing the issue resolution pipeline."""

    issue_id: str
    channel: ChannelType
    triage: TriageDecision
    branch_name: str = ""
    commit_sha: str = ""
    pr_url: str = ""
    verification: VerificationResult | None = None
    success: bool = False
    message: str = ""
