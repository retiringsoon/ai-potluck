"""orchestrator.py - Core pipeline coordinating triage, solve, verify, release, and notice."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from issue_agent.adapters.base import BaseChannelAdapter
from issue_agent.evaluator import IssueEvaluator
from issue_agent.git_ops import GitOps
from issue_agent.models import (
    Issue,
    IssueStatus,
    PipelineResult,
)
from issue_agent.solver import IssueSolver
from issue_agent.verifier import ProjectVerifier

logger = logging.getLogger("issue_agent.orchestrator")


class IssueOrchestrator:
    """End-to-end issue resolution orchestrator."""

    def __init__(
        self,
        adapter: BaseChannelAdapter,
        evaluator: IssueEvaluator | None = None,
        verifier: ProjectVerifier | None = None,
        git_ops: GitOps | None = None,
        solver: IssueSolver | None = None,
        project_root: str | Path | None = None,
        dry_run: bool = False,
    ) -> None:
        self.adapter = adapter
        if project_root:
            self.project_root = Path(project_root)
        else:
            self.project_root = Path(__file__).resolve().parent.parent.parent

        self.dry_run = dry_run
        self.evaluator = evaluator or IssueEvaluator()
        self.verifier = verifier or ProjectVerifier(self.project_root)
        self.git_ops = git_ops or GitOps(self.project_root, dry_run=self.dry_run)
        self.solver = solver or IssueSolver(self.project_root)

    def process_issue(
        self,
        issue_id: str,
        force_human_approve: bool = False,
        custom_fixer: Callable[[Issue, Path], list[str]] | None = None,
        specific_tests: list[str] | None = None,
        auto_close_issue: bool = False,
    ) -> PipelineResult:
        """Execute the full lifecycle for a given issue."""
        logger.info("=== Starting Issue Resolution Pipeline for #%s ===", issue_id)

        # 1. Fetch Issue
        issue = self.adapter.get_issue(issue_id)
        logger.info("Fetched issue '%s' by %s", issue.title, issue.author)

        # 2. Triage / Gate Decision
        triage = self.evaluator.evaluate(issue, force_human_approve=force_human_approve)
        logger.info("Triage result: %s", triage.summary())

        if not triage.approved:
            msg = f"Issue #{issue_id} not approved for autonomous resolution: {triage.reason}"
            logger.warning(msg)
            return PipelineResult(
                issue_id=issue_id,
                channel=self.adapter.channel_type,
                triage=triage,
                success=False,
                message=msg,
            )

        # 3. Update Issue State (In Progress)
        logger.info("Marking issue #%s as IN_PROGRESS and posting notice...", issue_id)
        self.adapter.update_issue_status(
            issue_id, IssueStatus.IN_PROGRESS, label="agent:in-progress"
        )
        start_comment = (
            f"🤖 **ai-potluck Issue Agent** has picked up this issue.\n\n"
            f"- **Triage Gate**: Approved by `{triage.source}` "
            f"(Confidence: {triage.confidence:.2f})\n"
            f"- **Complexity**: `{triage.complexity}`\n"
            f"- **Target Components**: {', '.join(triage.target_components) or 'general'}\n"
            f"- **Plan**: {triage.suggested_approach}\n\n"
            f"Starting reproduction, code fix, and evidence-based test suite."
        )
        self.adapter.post_comment(issue_id, start_comment)

        # 4. Prepare isolated Git branch
        original_branch = self.git_ops.get_current_branch()
        slug = GitOps.slugify(issue.title)
        branch_name = f"fix/issue-{issue.id}-{slug}"
        logger.info("Preparing branch '%s' (current: '%s')...", branch_name, original_branch)
        self.git_ops.create_and_checkout_branch(branch_name)

        try:
            # 5. Execute Fix
            logger.info("Applying fix for issue #%s...", issue_id)
            modified_files = self.solver.solve(issue, custom_fixer=custom_fixer)
            logger.info("Modified files: %s", modified_files)

            # 6. Verify (Evidence-based gate)
            logger.info("Running quality verification gates (ruff, mypy, pytest)...")
            verification = self.verifier.verify_all(specific_tests=specific_tests)

            if not verification.passed:
                err_msg = (
                    f"Verification gates failed for issue #{issue_id}!\n"
                    f"Ruff: {'OK' if verification.ruff_passed else 'FAILED'}\n"
                    f"Mypy: {'OK' if verification.mypy_passed else 'FAILED'}\n"
                    f"Pytest: {'OK' if verification.pytest_passed else 'FAILED'}\n"
                )
                logger.error(err_msg)
                fail_note = (
                    f"⚠️ **ai-potluck Issue Agent encountered verification failure**:\n\n"
                    f"```\n{verification.summary}\n```\n"
                    f"Reverting changes. Human assistance requested."
                )
                self.adapter.post_comment(issue_id, fail_note)
                self.git_ops.checkout(original_branch)
                return PipelineResult(
                    issue_id=issue_id,
                    channel=self.adapter.channel_type,
                    triage=triage,
                    branch_name=branch_name,
                    verification=verification,
                    success=False,
                    message=err_msg,
                )

            # 7. Commit & Release / PR
            component_tag = triage.target_components[0] if triage.target_components else "core"
            commit_msg = f"fix({component_tag}): {issue.title}\n\nResolves #{issue.id}"
            commit_sha = self.git_ops.stage_and_commit(modified_files, commit_msg)
            logger.info("Committed changes with SHA: %s", commit_sha)

            self.git_ops.push_branch(branch_name)

            pr_title = f"fix({component_tag}): {issue.title} (resolves #{issue.id})"
            pr_body = (
                f"## Summary\n"
                f"Automated resolution for issue #{issue.id} by ai-potluck Issue Agent.\n\n"
                f"### Changes\n"
                f"- Problem: {issue.title}\n"
                f"- Modified files: {', '.join(modified_files)}\n\n"
                f"### Verification Evidence\n"
                f"- Ruff check: {'PASS' if verification.ruff_passed else 'FAIL'}\n"
                f"- Mypy strict: {'PASS' if verification.mypy_passed else 'FAIL'}\n"
                f"- Pytest suite: {'PASS' if verification.pytest_passed else 'FAIL'}\n\n"
                f"Closes #{issue.id}\n"
            )

            pr_url = ""
            create_pr = getattr(self.adapter, "create_pull_request", None)
            if callable(create_pr):
                pr_url = create_pr(
                    head_branch=branch_name,
                    base_branch=original_branch or "main",
                    title=pr_title,
                    body=pr_body,
                )
            logger.info("PR creation output: %s", pr_url)

            # 8. Post Completion Notice on Issue & Update Status
            self.adapter.update_issue_status(
                issue_id, IssueStatus.RESOLVED, label="agent:resolved"
            )
            done_comment = (
                f"✅ **ai-potluck Issue Agent has fixed and verified this issue!**\n\n"
                f"- **Fix Branch**: `{branch_name}`\n"
                f"- **Commit**: `{commit_sha}`\n"
                f"- **Pull Request**: {pr_url or 'Created'}\n\n"
                f"### Verification Evidence\n"
                f"```text\n{verification.summary}\n```\n"
            )
            self.adapter.post_comment(issue_id, done_comment)

            if auto_close_issue:
                self.adapter.close_issue(issue_id, comment=f"Resolved automatically in {pr_url}")

            # Return to original branch
            self.git_ops.checkout(original_branch)

            return PipelineResult(
                issue_id=issue_id,
                channel=self.adapter.channel_type,
                triage=triage,
                branch_name=branch_name,
                commit_sha=commit_sha,
                pr_url=pr_url,
                verification=verification,
                success=True,
                message=f"Successfully resolved issue #{issue_id} and opened PR: {pr_url}",
            )

        except Exception as e:
            logger.exception("Unexpected exception in orchestrator for issue #%s: %s", issue_id, e)
            self.git_ops.checkout(original_branch)
            raise
