"""base.py - Base channel adapter contract for issue intake and lifecycle."""

from __future__ import annotations

from abc import ABC, abstractmethod

from issue_agent.models import ChannelType, Issue, IssueStatus


class BaseChannelAdapter(ABC):
    """Abstract base class for channel adapters (GitHub, Discord, TG, Feishu)."""

    @property
    @abstractmethod
    def channel_type(self) -> ChannelType:
        """The channel type represented by this adapter."""
        ...

    @abstractmethod
    def fetch_issues(
        self, state: str = "open", labels: list[str] | None = None
    ) -> list[Issue]:
        """Fetch a list of active issues matching criteria."""
        ...

    @abstractmethod
    def get_issue(self, issue_id: str) -> Issue:
        """Fetch details of a single issue by its identifier."""
        ...

    @abstractmethod
    def update_issue_status(
        self, issue_id: str, status: IssueStatus, label: str | None = None
    ) -> None:
        """Update an issue's status or label on the upstream channel."""
        ...

    @abstractmethod
    def post_comment(self, issue_id: str, comment: str) -> str:
        """Post a comment or update message to the issue thread."""
        ...

    @abstractmethod
    def close_issue(self, issue_id: str, comment: str | None = None) -> None:
        """Close the issue on upstream channel with an optional closing note."""
        ...
