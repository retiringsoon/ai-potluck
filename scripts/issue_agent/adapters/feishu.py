"""feishu.py - Reserved Feishu/Lark channel adapter interface."""

from __future__ import annotations

import logging

from issue_agent.adapters.base import BaseChannelAdapter
from issue_agent.models import ChannelType, Issue, IssueStatus

logger = logging.getLogger("issue_agent.feishu")


class FeishuAdapter(BaseChannelAdapter):
    """Reserved channel adapter for Feishu/Lark incident bot or card webhooks.

    To activate:
    1. Configure FEISHU_APP_ID, FEISHU_APP_SECRET, or FEISHU_WEBHOOK_URL.
    2. Map Feishu ticket cards or group mentions to Issue objects.
    3. Post interactive message cards for approval, progress tracking, and verification results.
    """

    def __init__(
        self,
        app_id: str | None = None,
        app_secret: str | None = None,
        webhook_url: str | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.webhook_url = webhook_url

    @property
    def channel_type(self) -> ChannelType:
        return ChannelType.FEISHU

    def fetch_issues(
        self, state: str = "open", labels: list[str] | None = None
    ) -> list[Issue]:
        raise NotImplementedError(
            "Feishu adapter intake interface is reserved. "
            "Configure Feishu webhook or app credentials to enable intake."
        )

    def get_issue(self, issue_id: str) -> Issue:
        raise NotImplementedError("Feishu adapter get_issue is reserved.")

    def update_issue_status(
        self, issue_id: str, status: IssueStatus, label: str | None = None
    ) -> None:
        raise NotImplementedError("Feishu adapter update_issue_status is reserved.")

    def post_comment(self, issue_id: str, comment: str) -> str:
        raise NotImplementedError("Feishu adapter post_comment is reserved.")

    def close_issue(self, issue_id: str, comment: str | None = None) -> None:
        raise NotImplementedError("Feishu adapter close_issue is reserved.")
