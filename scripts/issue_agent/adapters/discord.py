"""discord.py - Reserved Discord channel adapter interface."""

from __future__ import annotations

import logging

from issue_agent.adapters.base import BaseChannelAdapter
from issue_agent.models import ChannelType, Issue, IssueStatus

logger = logging.getLogger("issue_agent.discord")


class DiscordAdapter(BaseChannelAdapter):
    """Reserved channel adapter for Discord forum posts or support threads.

    To activate:
    1. Configure DISCORD_BOT_TOKEN and DISCORD_GUILD_ID.
    2. Map Discord forum threads or ticket channels to Issue objects.
    3. Use discord.py or raw Discord REST API to post updates and embeds.
    """

    def __init__(self, bot_token: str | None = None, guild_id: str | None = None) -> None:
        self.bot_token = bot_token
        self.guild_id = guild_id

    @property
    def channel_type(self) -> ChannelType:
        return ChannelType.DISCORD

    def fetch_issues(
        self, state: str = "open", labels: list[str] | None = None
    ) -> list[Issue]:
        raise NotImplementedError(
            "Discord adapter intake interface is reserved. "
            "Configure DISCORD_BOT_TOKEN to enable Discord forum thread polling."
        )

    def get_issue(self, issue_id: str) -> Issue:
        raise NotImplementedError("Discord adapter get_issue is reserved.")

    def update_issue_status(
        self, issue_id: str, status: IssueStatus, label: str | None = None
    ) -> None:
        raise NotImplementedError("Discord adapter update_issue_status is reserved.")

    def post_comment(self, issue_id: str, comment: str) -> str:
        raise NotImplementedError("Discord adapter post_comment is reserved.")

    def close_issue(self, issue_id: str, comment: str | None = None) -> None:
        raise NotImplementedError("Discord adapter close_issue is reserved.")
