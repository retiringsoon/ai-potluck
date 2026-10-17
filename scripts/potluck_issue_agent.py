#!/usr/bin/env python3
"""potluck_issue_agent.py - CLI Runner for the ai-potluck Issue Resolver & Release Agent.

Usage examples:
  # 1. List open issues from GitHub:
  ./scripts/potluck_issue_agent.py list

  # 2. Triage / evaluate an issue:
  ./scripts/potluck_issue_agent.py triage 12

  # 3. Force approve and execute pipeline (dry-run):
  ./scripts/potluck_issue_agent.py run 12 --force-human-approve --dry-run

  # 4. Autonomous agent execution (agent decides feasibility):
  ./scripts/potluck_issue_agent.py run 12 --auto-approve

  # 5. Continuous daemon mode:
  ./scripts/potluck_issue_agent.py daemon --interval 60 --auto-approve
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Add scripts directory to sys.path so issue_agent package can be imported
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from issue_agent.adapters.base import BaseChannelAdapter  # noqa: E402
from issue_agent.adapters.discord import DiscordAdapter  # noqa: E402
from issue_agent.adapters.feishu import FeishuAdapter  # noqa: E402
from issue_agent.adapters.github import GitHubAdapter  # noqa: E402
from issue_agent.adapters.telegram import TelegramAdapter  # noqa: E402
from issue_agent.evaluator import IssueEvaluator  # noqa: E402
from issue_agent.git_ops import GitOps  # noqa: E402
from issue_agent.orchestrator import IssueOrchestrator  # noqa: E402
from issue_agent.solver import IssueSolver  # noqa: E402
from issue_agent.verifier import ProjectVerifier  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("potluck_agent")


def get_adapter(
    channel_str: str, repo: str, dry_run: bool = False
) -> BaseChannelAdapter:
    ch = channel_str.lower()
    if ch in ("github", "gh"):
        return GitHubAdapter(repo=repo, dry_run=dry_run)
    elif ch == "discord":
        return DiscordAdapter()
    elif ch in ("telegram", "tg"):
        return TelegramAdapter()
    elif ch in ("feishu", "lark"):
        return FeishuAdapter()
    else:
        raise ValueError(
            f"Unknown channel '{channel_str}'. Choose from: github, discord, telegram, feishu"
        )


def cmd_list(args: argparse.Namespace) -> None:
    adapter = get_adapter(args.channel, args.repo, dry_run=args.dry_run)
    try:
        issues = adapter.fetch_issues(state=args.state)
    except NotImplementedError as e:
        print(f"\n[INFO] {e}\n")
        return

    print(f"\n--- Open Issues ({args.channel.upper()}: {args.repo}) ---")
    if not issues:
        print("No matching issues found.")
        return

    for iss in issues:
        labels_str = f"[{', '.join(iss.labels)}]" if iss.labels else ""
        print(f"#{iss.id:<4} {iss.title:<55} {labels_str} (@{iss.author})")
    print(f"Total: {len(issues)} issue(s)\n")


def cmd_triage(args: argparse.Namespace) -> None:
    adapter = get_adapter(args.channel, args.repo, dry_run=args.dry_run)
    evaluator = IssueEvaluator(auto_approve_threshold=args.threshold)

    try:
        issue = adapter.get_issue(args.issue_id)
    except NotImplementedError as e:
        print(f"\n[INFO] {e}\n")
        return

    decision = evaluator.evaluate(issue, force_human_approve=args.force_human_approve)

    print("\n================ ISSUE TRIAGE EVALUATION ================")
    print(f"Issue: #{issue.id} - {issue.title}")
    print(f"Author: @{issue.author} | URL: {issue.url}")
    print(f"Labels: {', '.join(issue.labels) or 'None'}")
    print("---------------------------------------------------------")
    print(f"Decision:    {'APPROVED' if decision.approved else 'NEEDS HUMAN REVIEW'}")
    print(f"Gate Source: {decision.source.upper()}")
    print(f"Confidence:  {decision.confidence:.2f} (Threshold: {args.threshold:.2f})")
    print(f"Complexity:  {decision.complexity.upper()}")
    print(f"Components:  {', '.join(decision.target_components) or 'general'}")
    print(f"Reason:      {decision.reason}")
    print(f"Approach:    {decision.suggested_approach}")
    print("=========================================================\n")


def cmd_run(args: argparse.Namespace) -> None:
    adapter = get_adapter(args.channel, args.repo, dry_run=args.dry_run)
    project_root = SCRIPTS_DIR.parent

    evaluator = IssueEvaluator(auto_approve_threshold=args.threshold)
    verifier = ProjectVerifier(project_root)
    git_ops = GitOps(project_root, dry_run=args.dry_run)
    solver = IssueSolver(project_root)

    orchestrator = IssueOrchestrator(
        adapter=adapter,
        evaluator=evaluator,
        verifier=verifier,
        git_ops=git_ops,
        solver=solver,
        project_root=project_root,
        dry_run=args.dry_run,
    )

    specific_tests = [args.test] if args.test else None

    result = orchestrator.process_issue(
        issue_id=args.issue_id,
        force_human_approve=args.force_human_approve,
        specific_tests=specific_tests,
        auto_close_issue=args.auto_close,
    )

    print("\n================ PIPELINE EXECUTION SUMMARY ================")
    print(f"Issue ID:     #{result.issue_id}")
    print(f"Status:       {'SUCCESS' if result.success else 'FAILED'}")
    print(f"Gate:         {result.triage.summary()}")
    if result.branch_name:
        print(f"Branch:       {result.branch_name}")
    if result.commit_sha:
        print(f"Commit:       {result.commit_sha}")
    if result.pr_url:
        print(f"Pull Request: {result.pr_url}")
    if result.verification:
        print("\n--- Quality Gates Evidence ---")
        print(result.verification.summary)
    print(f"\nResult: {result.message}")
    print("============================================================\n")


def cmd_daemon(args: argparse.Namespace) -> None:
    adapter = get_adapter(args.channel, args.repo, dry_run=args.dry_run)
    project_root = SCRIPTS_DIR.parent
    evaluator = IssueEvaluator(auto_approve_threshold=args.threshold)
    verifier = ProjectVerifier(project_root)
    git_ops = GitOps(project_root, dry_run=args.dry_run)
    solver = IssueSolver(project_root)

    orchestrator = IssueOrchestrator(
        adapter=adapter,
        evaluator=evaluator,
        verifier=verifier,
        git_ops=git_ops,
        solver=solver,
        project_root=project_root,
        dry_run=args.dry_run,
    )

    logger.info(
        "Starting Issue Agent Daemon on %s (%s). Poll interval: %ds",
        args.channel,
        args.repo,
        args.interval,
    )

    while True:
        try:
            issues = adapter.fetch_issues(state="open")
            for iss in issues:
                if "agent:in-progress" in iss.labels or "agent:resolved" in iss.labels:
                    continue

                triage = evaluator.evaluate(iss, force_human_approve=False)
                if triage.approved:
                    logger.info("Daemon picked up auto-approved issue #%s: %s", iss.id, iss.title)
                    orchestrator.process_issue(iss.id, force_human_approve=False)
                else:
                    logger.debug(
                        "Skipping issue #%s (needs human review): %s", iss.id, triage.reason
                    )
        except Exception as e:
            logger.error("Daemon cycle error: %s", e)

        time.sleep(args.interval)


def main() -> None:
    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument(
        "--repo",
        default="retiringsoon/ai-potluck",
        help="Target GitHub repository (owner/name)",
    )
    common_parser.add_argument(
        "--channel",
        default="github",
        choices=["github", "discord", "telegram", "feishu"],
        help="Intake channel source",
    )
    common_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate remote Git push, PR creation, and issue edits without side-effects",
    )
    common_parser.add_argument(
        "--threshold",
        type=float,
        default=0.70,
        help="Autonomous agent approval confidence threshold (0.0 - 1.0)",
    )

    parser = argparse.ArgumentParser(
        description="ai-potluck Issue Resolver & Release Agent",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        parents=[common_parser],
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = subparsers.add_parser(
        "list", help="List active issues", parents=[common_parser]
    )
    p_list.add_argument("--state", default="open", choices=["open", "closed", "all"])

    # triage
    p_triage = subparsers.add_parser(
        "triage",
        help="Evaluate issue feasibility and approval gate",
        parents=[common_parser],
    )
    p_triage.add_argument("issue_id", help="Issue number or identifier")
    p_triage.add_argument(
        "--force-human-approve",
        action="store_true",
        help="Force human confirmation override",
    )

    # run
    p_run = subparsers.add_parser(
        "run",
        help="Run full pipeline: triage -> fix -> test -> release",
        parents=[common_parser],
    )
    p_run.add_argument("issue_id", help="Issue number or identifier")
    p_run.add_argument(
        "--force-human-approve",
        action="store_true",
        help="Manually confirm and approve resolution regardless of agent score",
    )
    p_run.add_argument(
        "--auto-approve",
        action="store_true",
        help="Allow agent autonomous evaluation to proceed if threshold is met",
    )
    p_run.add_argument(
        "--test",
        default=None,
        help="Optional specific test target path (e.g. tests/test_cli.py)",
    )
    p_run.add_argument(
        "--auto-close",
        action="store_true",
        help="Automatically close issue after PR creation",
    )

    # daemon
    p_daemon = subparsers.add_parser(
        "daemon",
        help="Watch channel and auto-resolve approved issues",
        parents=[common_parser],
    )
    p_daemon.add_argument("--interval", type=int, default=60, help="Polling interval in seconds")

    args = parser.parse_args()

    if args.command == "list":
        cmd_list(args)
    elif args.command == "triage":
        cmd_triage(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "daemon":
        cmd_daemon(args)


if __name__ == "__main__":
    main()
