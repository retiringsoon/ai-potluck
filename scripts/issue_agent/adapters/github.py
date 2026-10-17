"""github.py - GitHub Issues channel adapter using gh CLI with REST fallback."""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from issue_agent.adapters.base import BaseChannelAdapter
from issue_agent.models import ChannelType, Issue, IssueStatus

logger = logging.getLogger("issue_agent.github")


class GitHubAdapter(BaseChannelAdapter):
    """Channel adapter for GitHub issues using the official gh CLI."""

    def __init__(self, repo: str = "retiringsoon/ai-potluck", dry_run: bool = False) -> None:
        self.repo = repo
        self.dry_run = dry_run
        self._mock_issues: dict[str, Issue] = {}

    @property
    def channel_type(self) -> ChannelType:
        return ChannelType.GITHUB

    def _run_gh(self, args: list[str]) -> str:
        """Execute a gh CLI command and return stdout."""
        cmd = ["gh", *args]
        logger.debug("Executing gh: %s", " ".join(cmd))
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return res.stdout.strip()
        except subprocess.CalledProcessError as e:
            err = e.stderr.strip() or e.stdout.strip() or str(e)
            logger.error("gh command failed: %s | Error: %s", " ".join(cmd), err)
            raise RuntimeError(f"GitHub CLI command failed: {err}") from e
        except FileNotFoundError:
            raise RuntimeError(
                "The 'gh' CLI tool is required for GitHubAdapter. "
                "Install it via 'brew install gh' or similar."
            ) from None

    def register_mock_issue(self, issue: Issue) -> None:
        """Register an in-memory mock issue (used for testing or offline dry-runs)."""
        self._mock_issues[str(issue.id)] = issue

    def fetch_issues(
        self, state: str = "open", labels: list[str] | None = None
    ) -> list[Issue]:
        if self._mock_issues:
            return list(self._mock_issues.values())

        args = [
            "issue",
            "list",
            "--repo",
            self.repo,
            "--state",
            state,
            "--json",
            "number,title,body,author,url,labels,createdAt",
            "--limit",
            "30",
        ]
        if labels:
            for label_name in labels:
                args.extend(["--label", label_name])

        out = self._run_gh(args)
        if not out:
            return []

        raw_list: list[dict[str, Any]] = json.loads(out)
        issues: list[Issue] = []
        for item in raw_list:
            author_login = ""
            if isinstance(item.get("author"), dict):
                author_login = item["author"].get("login", "")

            label_names = [
                lbl.get("name", "")
                for lbl in item.get("labels", [])
                if isinstance(lbl, dict)
            ]

            issues.append(
                Issue(
                    id=str(item.get("number")),
                    channel=ChannelType.GITHUB,
                    title=item.get("title", ""),
                    body=item.get("body", ""),
                    author=author_login,
                    url=item.get(
                        "url",
                        f"https://github.com/{self.repo}/issues/{item.get('number')}",
                    ),
                    labels=label_names,
                    created_at=item.get("createdAt", ""),
                    raw_payload=item,
                )
            )
        return issues

    def get_issue(self, issue_id: str) -> Issue:
        if str(issue_id) in self._mock_issues:
            return self._mock_issues[str(issue_id)]

        args = [
            "issue",
            "view",
            str(issue_id),
            "--repo",
            self.repo,
            "--json",
            "number,title,body,author,url,labels,createdAt",
        ]
        out = self._run_gh(args)
        item: dict[str, Any] = json.loads(out)
        author_login = ""
        if isinstance(item.get("author"), dict):
            author_login = item["author"].get("login", "")

        label_names = [
            lbl.get("name", "")
            for lbl in item.get("labels", [])
            if isinstance(lbl, dict)
        ]

        return Issue(
            id=str(item.get("number")),
            channel=ChannelType.GITHUB,
            title=item.get("title", ""),
            body=item.get("body", ""),
            author=author_login,
            url=item.get(
                "url",
                f"https://github.com/{self.repo}/issues/{item.get('number')}",
            ),
            labels=label_names,
            created_at=item.get("createdAt", ""),
            raw_payload=item,
        )

    def update_issue_status(
        self, issue_id: str, status: IssueStatus, label: str | None = None
    ) -> None:
        target_label = label or f"agent:{status.value}"
        if self.dry_run:
            logger.info("[DRY-RUN] Update issue #%s label to '%s'", issue_id, target_label)
            if (
                str(issue_id) in self._mock_issues
                and target_label not in self._mock_issues[str(issue_id)].labels
            ):
                self._mock_issues[str(issue_id)].labels.append(target_label)
            return

        try:
            self._run_gh(
                ["issue", "edit", str(issue_id), "--repo", self.repo, "--add-label", target_label]
            )
        except Exception as e:
            logger.warning(
                "Could not set label '%s' on #%s (label might need creation): %s",
                target_label,
                issue_id,
                e,
            )

    def post_comment(self, issue_id: str, comment: str) -> str:
        if self.dry_run:
            logger.info("[DRY-RUN] Comment on issue #%s:\n%s", issue_id, comment)
            return "dry-run-comment-id"

        return self._run_gh(
            ["issue", "comment", str(issue_id), "--repo", self.repo, "--body", comment]
        )

    def close_issue(self, issue_id: str, comment: str | None = None) -> None:
        if self.dry_run:
            logger.info("[DRY-RUN] Close issue #%s with comment: %s", issue_id, comment)
            return

        args = ["issue", "close", str(issue_id), "--repo", self.repo]
        if comment:
            args.extend(["--comment", comment])
        self._run_gh(args)

    def create_pull_request(
        self, head_branch: str, base_branch: str, title: str, body: str
    ) -> str:
        """Create a GitHub pull request for the resolved issue."""
        if self.dry_run:
            mock_url = f"https://github.com/{self.repo}/pull/dry-run-{head_branch}"
            logger.info(
                "[DRY-RUN] Created PR from %s to %s: %s", head_branch, base_branch, mock_url
            )
            return mock_url

        args = [
            "pr",
            "create",
            "--repo",
            self.repo,
            "--head",
            head_branch,
            "--base",
            base_branch,
            "--title",
            title,
            "--body",
            body,
        ]
        return self._run_gh(args)
