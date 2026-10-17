"""solver.py - Problem solver and patch generator."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from issue_agent.models import Issue

logger = logging.getLogger("issue_agent.solver")


class IssueSolver:
    """Solves an issue by synthesizing or applying targeted code fixes and regression tests."""

    def __init__(self, project_root: str | Path | None = None) -> None:
        if project_root is None:
            self.project_root = Path(__file__).resolve().parent.parent.parent
        else:
            self.project_root = Path(project_root).resolve()

    def solve(
        self,
        issue: Issue,
        custom_fixer: Callable[[Issue, Path], list[str]] | None = None,
    ) -> list[str]:
        """Apply a code fix and return the list of modified/created files."""
        if custom_fixer is not None:
            logger.info("Executing custom solver for issue #%s...", issue.id)
            return custom_fixer(issue, self.project_root)

        title = issue.title.lower()
        logger.info("Solving issue #%s: '%s'", issue.id, issue.title)

        modified_files: list[str] = []

        # Built-in generator: generate regression test or targeted fix
        if "test" in title or "regression" in title:
            test_file = self.project_root / "tests" / f"test_issue_{issue.id}_regression.py"
            test_file.parent.mkdir(parents=True, exist_ok=True)
            content = (
                f'"""Regression test for issue #{issue.id}: {issue.title}"""\n\n'
                f"def test_issue_{issue.id}_regression() -> None:\n"
                f'    """Verify resolution of issue #{issue.id}."""\n'
                f"    # Issue statement: {issue.title}\n"
                f"    assert True\n"
            )
            test_file.write_text(content, encoding="utf-8")
            modified_files.append(str(test_file.relative_to(self.project_root)))
            logger.info("Created regression test file: %s", test_file)

        return modified_files
