"""telegram.py - Reserved Telegram channel adapter interface."""

from __future__ import annotations

import logging

from issue_agent.adapters.base import BaseChannelAdapter
from issue_agent.models import ChannelType, Issue, IssueStatus

logger = logging.getLogger("issue_agent.telegram")


class TelegramAdapter(BaseChannelAdapter):
    """Reserved channel adapter for Telegram group topic issues or support tickets.

    To activate:
    1. Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
    2. Map group forum topics or /bug command tickets to Issue objects.
    3. Use telegram Bot API (sendMessage, editMessageText) to post status updates.
    """

    def __init__(self, bot_token: str | None = None, chat_id: str | None = None) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id

    @property
    def channel_type(self) -> ChannelType:
        return ChannelType.TELEGRAM

    def fetch_issues(
        self, state: str = "open", labels: list[str] | None = None
    ) -> list[Issue]:
        raise NotImplementedError(
            "Telegram adapter intake interface is reserved. "
            "Configure TELEGRAM_BOT_TOKEN to enable Telegram ticket intake."
        )

    def get_issue(self, issue_id: str) -> Issue:
        raise NotImplementedError("Telegram adapter get_issue is reserved.")

    def update_issue_status(
        self, issue_id: str, status: IssueStatus, label: str | None = None
    ) -> None:
        raise NotImplementedError("Telegram adapter update_issue_status is reserved.")

    def post_comment(self, issue_id: str, comment: str) -> str:
        raise NotImplementedError("Telegram adapter post_comment is reserved.")

    def close_issue(self, issue_id: str, comment: str | None = None) -> None:
        raise NotImplementedError("Telegram adapter close_issue is reserved.")
