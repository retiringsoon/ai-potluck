"""adapters - Channel adapters for issue sources."""

from issue_agent.adapters.base import BaseChannelAdapter
from issue_agent.adapters.discord import DiscordAdapter
from issue_agent.adapters.feishu import FeishuAdapter
from issue_agent.adapters.github import GitHubAdapter
from issue_agent.adapters.telegram import TelegramAdapter

__all__ = [
    "BaseChannelAdapter",
    "DiscordAdapter",
    "FeishuAdapter",
    "GitHubAdapter",
    "TelegramAdapter",
]
