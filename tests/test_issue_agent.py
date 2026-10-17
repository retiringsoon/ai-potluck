"""test_issue_agent.py - Comprehensive tests for the ai-potluck Issue Resolver Agent."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure scripts dir is on sys.path
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from issue_agent.adapters.discord import DiscordAdapter  # noqa: E402
from issue_agent.adapters.feishu import FeishuAdapter  # noqa: E402
from issue_agent.adapters.github import GitHubAdapter  # noqa: E402
from issue_agent.adapters.telegram import TelegramAdapter  # noqa: E402
from issue_agent.evaluator import IssueEvaluator  # noqa: E402
from issue_agent.git_ops import GitOps  # noqa: E402
from issue_agent.models import (  # noqa: E402
    ChannelType,
    Issue,
    IssueStatus,
    TriageDecision,
    VerificationResult,
)
from issue_agent.orchestrator import IssueOrchestrator  # noqa: E402
from issue_agent.solver import IssueSolver  # noqa: E402
from issue_agent.verifier import ProjectVerifier  # noqa: E402


def test_models_representation() -> None:
    issue = Issue(
        id="42",
        channel=ChannelType.GITHUB,
        title="Fix CLI help flag output",
        body="CLI --help output has a minor typo in description.",
        author="alice",
        url="https://github.com/retiringsoon/ai-potluck/issues/42",
        labels=["bug", "cli"],
    )
    assert issue.full_key == "github#42"

    triage = TriageDecision(
        approved=True,
        source="agent",
        confidence=0.85,
        reason="Actionable CLI issue",
        complexity="low",
        target_components=["cli"],
    )
    assert "APPROVED" in triage.summary()
    assert "AGENT" in triage.summary()

    verif = VerificationResult(
        passed=True,
        ruff_passed=True,
        mypy_passed=True,
        pytest_passed=True,
        summary="All gates passed",
    )
    assert verif.passed is True


def test_reserved_adapters_raise_not_implemented() -> None:
    """Verify that reserved channels (Discord, TG, Feishu) fail safely."""
    discord = DiscordAdapter()
    assert discord.channel_type == ChannelType.DISCORD
    with pytest.raises(NotImplementedError, match="Discord adapter intake interface is reserved"):
        discord.fetch_issues()

    tg = TelegramAdapter()
    assert tg.channel_type == ChannelType.TELEGRAM
    with pytest.raises(NotImplementedError, match="Telegram adapter intake interface is reserved"):
        tg.fetch_issues()

    feishu = FeishuAdapter()
    assert feishu.channel_type == ChannelType.FEISHU
    with pytest.raises(NotImplementedError, match="Feishu adapter intake interface is reserved"):
        feishu.fetch_issues()


def test_evaluator_human_approval() -> None:
    evaluator = IssueEvaluator(auto_approve_threshold=0.70)

    # 1. Flag override
    vague_issue = Issue(
        id="1",
        channel=ChannelType.GITHUB,
        title="Help needed",
        body="something wrong",
        author="bob",
        url="http://example.com/1",
    )
    res_flag = evaluator.evaluate(vague_issue, force_human_approve=True)
    assert res_flag.approved is True
    assert res_flag.source == "human"
    assert res_flag.confidence == 1.0

    # 2. Issue label override
    labeled_issue = Issue(
        id="2",
        channel=ChannelType.GITHUB,
        title="Help needed",
        body="something wrong",
        author="bob",
        url="http://example.com/2",
        labels=["bot:approved"],
    )
    res_label = evaluator.evaluate(labeled_issue)
    assert res_label.approved is True
    assert res_label.source == "human"

    # 3. Comment command override
    res_comment = evaluator.evaluate(vague_issue, comments=["@bot /fix please investigate"])
    assert res_comment.approved is True
    assert res_comment.source == "human"


def test_evaluator_agent_autonomous() -> None:
    evaluator = IssueEvaluator(auto_approve_threshold=0.70)

    # Clear, well-defined bug with error trace and component
    good_issue = Issue(
        id="10",
        channel=ChannelType.GITHUB,
        title="CLI parser fails on --config option with traceback",
        body=(
            "When running `potluck --config foo.json`, the CLI raises a KeyError.\n"
            "Traceback (most recent call last):\n"
            "  File aipotluck/cli.py line 40\n"
            "KeyError: 'config'\n"
            "Expected: graceful exit with error message."
        ),
        author="developer",
        url="http://example.com/10",
    )
    res = evaluator.evaluate(good_issue)
    assert res.approved is True
    assert res.source == "agent"
    assert res.confidence >= 0.70
    assert "cli" in res.target_components
    assert res.complexity == "low"

    # Strictly guarded invariant (breaks signing semantics)
    dangerous_issue = Issue(
        id="11",
        channel=ChannelType.GITHUB,
        title="Change domain separator for ed25519 signing",
        body="We should change the domain separator string to support another format.",
        author="stranger",
        url="http://example.com/11",
    )
    res_danger = evaluator.evaluate(dangerous_issue)
    assert res_danger.approved is False
    assert res_danger.complexity == "high"
    assert "strict trust-root invariant" in res_danger.reason


def test_git_ops_slugify() -> None:
    assert GitOps.slugify("Fix CLI Parser Bug #123!") == "fix-cli-parser-bug-123"
    assert GitOps.slugify("A" * 50) == "a" * 30


def test_github_adapter_mock_and_dry_run() -> None:
    adapter = GitHubAdapter(repo="retiringsoon/ai-potluck", dry_run=True)
    mock_iss = Issue(
        id="99",
        channel=ChannelType.GITHUB,
        title="Test issue",
        body="Test description",
        author="alice",
        url="http://github.com/retiringsoon/ai-potluck/issues/99",
    )
    adapter.register_mock_issue(mock_iss)

    fetched = adapter.get_issue("99")
    assert fetched.id == "99"

    # Update status
    adapter.update_issue_status("99", IssueStatus.IN_PROGRESS)
    assert "agent:in_progress" in mock_iss.labels

    # Comments and PR in dry run
    comment_res = adapter.post_comment("99", "Hello agent")
    assert comment_res == "dry-run-comment-id"

    pr_url = adapter.create_pull_request("fix/branch", "main", "PR Title", "PR Body")
    assert "dry-run" in pr_url


def test_orchestrator_end_to_end_flow(tmp_path: Path) -> None:
    """Run simulated pipeline: intake -> triage -> solve -> verify -> commit -> PR -> update."""
    adapter = GitHubAdapter(repo="retiringsoon/ai-potluck", dry_run=True)
    test_issue = Issue(
        id="101",
        channel=ChannelType.GITHUB,
        title="Add regression test for CLI error handler",
        body="We need a regression test verifying CLI error UX.\n```python\nassert True\n```",
        author="charlie",
        url="http://github.com/retiringsoon/ai-potluck/issues/101",
    )
    adapter.register_mock_issue(test_issue)

    # Create dummy git repo in tmp_path
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "agent@example.com"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=tmp_path, check=True)

    # Mock verifier to pass
    mock_verifier = MagicMock(spec=ProjectVerifier)
    mock_verifier.verify_all.return_value = VerificationResult(
        passed=True,
        ruff_passed=True,
        mypy_passed=True,
        pytest_passed=True,
        summary="Ruff: PASS\nMypy: PASS\nPytest: PASS",
    )

    git_ops = GitOps(repo_dir=tmp_path, dry_run=False)
    # mock push to not hit network
    git_ops.push_branch = MagicMock()  # type: ignore[assignment]

    solver = IssueSolver(project_root=tmp_path)
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)

    orchestrator = IssueOrchestrator(
        adapter=adapter,
        evaluator=IssueEvaluator(auto_approve_threshold=0.60),
        verifier=mock_verifier,
        git_ops=git_ops,
        solver=solver,
        project_root=tmp_path,
        dry_run=True,
    )

    result = orchestrator.process_issue("101", force_human_approve=False)

    assert result.success is True
    assert result.issue_id == "101"
    assert result.triage.approved is True
    assert result.branch_name.startswith("fix/issue-101-")
    assert result.commit_sha != ""
    assert "dry-run" in result.pr_url
    assert "agent:resolved" in test_issue.labels


def test_orchestrator_verification_failure_rollback(tmp_path: Path) -> None:
    """Verify that when verification fails, orchestrator reports failure and rolls back branch."""
    adapter = GitHubAdapter(repo="retiringsoon/ai-potluck", dry_run=True)
    test_issue = Issue(
        id="102",
        channel=ChannelType.GITHUB,
        title="Add failing test",
        body="A test that causes ruff or pytest to fail.",
        author="dan",
        url="http://github.com/retiringsoon/ai-potluck/issues/102",
        labels=["bot:approved"],
    )
    adapter.register_mock_issue(test_issue)

    # Git init
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "agent@example.com"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=tmp_path, check=True)

    # Mock verifier to fail
    mock_verifier = MagicMock(spec=ProjectVerifier)
    mock_verifier.verify_all.return_value = VerificationResult(
        passed=False,
        ruff_passed=False,
        mypy_passed=True,
        pytest_passed=True,
        summary="Ruff check failed: syntax error.",
    )

    git_ops = GitOps(repo_dir=tmp_path, dry_run=False)
    solver = IssueSolver(project_root=tmp_path)

    orchestrator = IssueOrchestrator(
        adapter=adapter,
        verifier=mock_verifier,
        git_ops=git_ops,
        solver=solver,
        project_root=tmp_path,
        dry_run=True,
    )

    result = orchestrator.process_issue("102", force_human_approve=True)

    assert result.success is False
    assert result.verification is not None
    assert result.verification.passed is False
    # Verified that branch returned to main
    assert git_ops.get_current_branch() == "main"
