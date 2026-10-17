"""verifier.py - Evidence-based test and quality verifier for ai-potluck."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from issue_agent.models import VerificationResult

logger = logging.getLogger("issue_agent.verifier")


class ProjectVerifier:
    """Executes the canonical quality and test gates mandated by AGENTS.md."""

    def __init__(self, project_root: str | Path | None = None) -> None:
        if project_root is None:
            # Default to parent directory of scripts/ (i.e. apps/ai-potluck)
            self.project_root = Path(__file__).resolve().parent.parent.parent
        else:
            self.project_root = Path(project_root).resolve()

    def run_ruff(self) -> tuple[bool, str]:
        """Run ruff check across the project."""
        cmd = ["uv", "run", "ruff", "check", "src/", "tests/"]
        logger.info("Running ruff check: %s", " ".join(cmd))
        try:
            res = subprocess.run(
                cmd,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=60,
            )
            passed = res.returncode == 0
            output = res.stdout.strip() or res.stderr.strip()
            return passed, output
        except Exception as e:
            return False, f"Ruff execution error: {e}"

    def run_mypy(self) -> tuple[bool, str]:
        """Run mypy strict check."""
        cmd = ["uv", "run", "mypy"]
        logger.info("Running mypy: %s", " ".join(cmd))
        try:
            res = subprocess.run(
                cmd,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=120,
            )
            passed = res.returncode == 0
            output = res.stdout.strip() or res.stderr.strip()
            return passed, output
        except Exception as e:
            return False, f"Mypy execution error: {e}"

    def run_pytest(self, test_targets: list[str] | None = None) -> tuple[bool, str]:
        """Run pytest with the mandatory --basetemp flag."""
        targets = test_targets or ["tests/"]
        cmd = [
            "uv",
            "run",
            "pytest",
            *targets,
            "-q",
            "--basetemp=.pytest_bt",
        ]
        logger.info("Running pytest: %s", " ".join(cmd))
        try:
            res = subprocess.run(
                cmd,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=180,
            )
            passed = res.returncode == 0
            output = res.stdout.strip() or res.stderr.strip()
            return passed, output
        except Exception as e:
            return False, f"Pytest execution error: {e}"

    def verify_all(
        self, fast_only: bool = False, specific_tests: list[str] | None = None
    ) -> VerificationResult:
        """Run all gates in sequence: ruff -> mypy -> pytest."""
        ruff_ok, ruff_out = self.run_ruff()
        if not ruff_ok and fast_only:
            return VerificationResult(
                passed=False,
                ruff_passed=False,
                mypy_passed=False,
                pytest_passed=False,
                ruff_output=ruff_out,
                summary="Ruff check failed.",
            )

        mypy_ok, mypy_out = self.run_mypy()
        if not mypy_ok and fast_only:
            return VerificationResult(
                passed=False,
                ruff_passed=ruff_ok,
                mypy_passed=False,
                pytest_passed=False,
                ruff_output=ruff_out,
                mypy_output=mypy_out,
                summary="Mypy check failed.",
            )

        pytest_ok, pytest_out = self.run_pytest(test_targets=specific_tests)

        all_ok = ruff_ok and mypy_ok and pytest_ok
        summary_lines = [
            f"Ruff check: {'PASS' if ruff_ok else 'FAIL'}",
            f"Mypy strict: {'PASS' if mypy_ok else 'FAIL'}",
            f"Pytest: {'PASS' if pytest_ok else 'FAIL'}",
        ]

        return VerificationResult(
            passed=all_ok,
            ruff_passed=ruff_ok,
            mypy_passed=mypy_ok,
            pytest_passed=pytest_ok,
            ruff_output=ruff_out,
            mypy_output=mypy_out,
            pytest_output=pytest_out,
            summary="\n".join(summary_lines),
        )
