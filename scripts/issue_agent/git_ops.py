"""git_ops.py - Git branch, commit, push, and PR operations."""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger("issue_agent.git_ops")


class GitOps:
    """Manages Git worktree/branch operations, commits, and PR releases."""

    def __init__(self, repo_dir: str | Path | None = None, dry_run: bool = False) -> None:
        if repo_dir is None:
            self.repo_dir = Path(__file__).resolve().parent.parent.parent
        else:
            self.repo_dir = Path(repo_dir).resolve()
        self.dry_run = dry_run

    def _run_git(self, args: list[str]) -> str:
        cmd = ["git", *args]
        logger.debug("Executing git: %s", " ".join(cmd))
        try:
            res = subprocess.run(
                cmd,
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            return res.stdout.strip()
        except subprocess.CalledProcessError as e:
            err = e.stderr.strip() or e.stdout.strip() or str(e)
            logger.error("Git command failed: %s | Error: %s", " ".join(cmd), err)
            raise RuntimeError(f"Git command failed: {err}") from e

    def get_current_branch(self) -> str:
        return self._run_git(["branch", "--show-current"])

    def create_and_checkout_branch(self, branch_name: str) -> None:
        """Create and switch to a dedicated fix branch."""
        if self.dry_run:
            logger.info("[DRY-RUN] Create and switch to branch: %s", branch_name)
            return
        # If branch already exists, checkout; otherwise checkout -b
        existing = self._run_git(["branch", "--list", branch_name])
        if existing:
            self._run_git(["checkout", branch_name])
        else:
            self._run_git(["checkout", "-b", branch_name])

    def checkout(self, branch_name: str) -> None:
        """Switch to an existing branch."""
        if self.dry_run:
            logger.info("[DRY-RUN] Switch to branch: %s", branch_name)
            return
        self._run_git(["checkout", branch_name])

    def has_uncommitted_changes(self) -> bool:
        """Check if tracked files have diffs."""
        out = self._run_git(["status", "--porcelain"])
        return len(out.strip()) > 0

    def stage_and_commit(self, files: list[str] | None, message: str) -> str:
        """Stage modified files and commit them."""
        if self.dry_run:
            logger.info("[DRY-RUN] Stage %s and commit: %s", files or "all modified", message)
            return "dry-run-commit-sha"

        if files:
            self._run_git(["add", *files])
        else:
            self._run_git(["add", "-u"])

        self._run_git(["commit", "-m", message])
        sha = self._run_git(["rev-parse", "HEAD"])
        return sha

    def push_branch(self, branch_name: str, remote: str = "origin") -> None:
        """Push the fix branch to the remote repository."""
        if self.dry_run:
            logger.info("[DRY-RUN] Push branch %s to %s", branch_name, remote)
            return
        self._run_git(["push", "-u", remote, branch_name])

    @staticmethod
    def slugify(text: str) -> str:
        """Create a clean git-branch-safe slug."""
        text = text.lower()
        text = re.sub(r"[^a-z0-9]+", "-", text)
        return text.strip("-")[:30]
