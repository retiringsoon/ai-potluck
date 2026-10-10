#!/usr/bin/env python3
"""The first-batch support matrix and the pre-install check derived from it (F03).

Why this file exists: an install that fails halfway leaves a half-made prefix and
a user with no idea whether their machine is the problem or the project is. The
matrix — which OS/architecture combinations and which CPython versions the
project actually builds, tests and supports — is therefore one table here, and
``install.sh`` asks this module before it creates anything: an unsupported
machine is refused up front, with the matrix and the concrete next step printed,
instead of dying inside ``uv`` or ``pip`` after a directory exists.

The table is also the single source for the two READMEs' "Supported
environments" section: a row added here is a row that has to appear there, and a
test in ``tests/test_release_support_matrix.py`` pins both the table and the
refusals, offline.

Stdlib only, and importable by an interpreter that has nothing installed: this
runs *before* the virtualenv exists, from a checkout, on a machine that may never
have seen this project. It must therefore stay readable by the oldest CPython a
supported machine may ship as ``python3`` (3.9).

Subcommand::

    preflight [--python X.Y] [--source SPEC]

It prints ``SUPPORT_<KEY>=<value>`` lines on stdout and human text on stderr, so
a POSIX ``sh`` caller can read one value with ``sed`` instead of parsing JSON.

Exit codes: 0 the checks passed, 2 a usage problem, 3 refused.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass

#: CPython versions the first batch is built and tested against. ``pyproject``'s
#: ``requires-python`` is deliberately looser (``>=3.11``): it states what the
#: code can run on, while this tuple states what a release is *verified* on and
#: what ``install.sh`` will build an environment for. A CPython outside it is
#: refused with the matrix printed, not installed and hoped for.
SUPPORTED_PYTHONS: tuple[str, ...] = ("3.11", "3.12", "3.13")

#: The version an install asks ``uv`` for when this machine has none of them yet.
#: ``uv`` resolves it locally or fetches a managed build, so the default is a
#: version, not a path.
DEFAULT_PYTHON: str = SUPPORTED_PYTHONS[-1]

#: How long one probe of an external tool may take. A hanging ``uv`` (a network
#: home directory, a broken shim) must not hang an install.
PROBE_TIMEOUT_S: float = 20.0

#: The prefix every key printed for a shell caller carries.
KEY_PREFIX: str = "SUPPORT_"


@dataclass(frozen=True)
class Target:
    """One supported OS/architecture combination.

    Attributes:
        target_id: The short id used in messages and tests.
        label: Human description, used in the matrix listing.
        system: The ``uname -s`` value this target matches.
        machines: The ``uname -m`` values this target matches.
    """

    target_id: str
    label: str
    system: str
    machines: tuple[str, ...]


#: The whole first-batch matrix. Adding a row here is what "supporting" a new
#: platform means: the installer's preflight, the release gate and the README
#: listing all follow this table.
TARGETS: tuple[Target, ...] = (
    Target(
        target_id="macos_arm64",
        label="macOS 11 or newer on Apple Silicon (arm64)",
        system="Darwin",
        machines=("arm64",),
    ),
    Target(
        target_id="linux_x86_64",
        label="Linux x86_64 (glibc 2.17 or newer)",
        system="Linux",
        machines=("x86_64", "amd64"),
    ),
    Target(
        target_id="linux_arm64",
        label="Linux arm64 (glibc 2.17 or newer)",
        system="Linux",
        machines=("aarch64", "arm64"),
    ),
)

#: Combinations that are refused on purpose, named so a refusal can say what the
#: user should do rather than only what they cannot do.
UNSUPPORTED_NOTES: tuple[str, ...] = (
    "Windows natively - run inside WSL2 (the installer is POSIX sh)",
    "macOS on Intel (x86_64) - not built or tested for this batch",
    f"CPython older than {SUPPORTED_PYTHONS[0]} or newer than {SUPPORTED_PYTHONS[-1]}",
    "PyPy, GraalPy and other non-CPython interpreters",
)


def target_of(target_id: str) -> Target | None:
    """Return the target with this id, or ``None`` when it is not supported."""
    for target in TARGETS:
        if target.target_id == target_id:
            return target
    return None


def supported_target_ids() -> tuple[str, ...]:
    """Return every target id in the matrix, in documented order."""
    return tuple(target.target_id for target in TARGETS)


def host_target(system: str | None = None, machine: str | None = None) -> Target | None:
    """Return the matrix row this machine belongs to, or ``None``.

    Args:
        system: Overrides ``platform.system()`` (tests).
        machine: Overrides ``platform.machine()`` (tests).
    """
    actual_system = platform.system() if system is None else system
    actual_machine = platform.machine() if machine is None else machine
    for target in TARGETS:
        if target.system == actual_system and actual_machine in target.machines:
            return target
    return None


def matrix_lines() -> tuple[str, ...]:
    """Return the support matrix as printable lines for guidance messages.

    The same lines back the READMEs' supported/not-supported listing, so the
    answer a refused user gets and the answer a reader gets cannot drift.
    """
    versions = "/".join(SUPPORTED_PYTHONS)
    lines = [f"  {target.target_id}: {target.label}, CPython {versions}" for target in TARGETS]
    lines.extend(f"  not supported: {note}" for note in UNSUPPORTED_NOTES)
    return tuple(lines)


def is_supported_python(version: str) -> bool:
    """Whether a ``X.Y`` CPython version is inside the matrix."""
    return version in SUPPORTED_PYTHONS


def _run(path: str, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run one probe, returning ``None`` when the tool will not run."""
    try:
        return subprocess.run(
            [path, *args],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def uv_path() -> str:
    """Return the ``uv`` executable, or ``""`` when it is not installed."""
    return shutil.which("uv") or ""


def uv_python(version: str) -> str:
    """Return the interpreter ``uv`` would build a venv from, or ``""``.

    ``--system`` skips virtualenvs (a project's ``.venv`` is not a base
    interpreter, and inside a checkout it is what ``uv`` would otherwise report)
    and ``--no-python-downloads`` keeps this probe from fetching anything: the
    question here is what this machine already has.
    """
    uv = uv_path()
    if not uv:
        return ""
    completed = _run(uv, ["python", "find", "--system", "--no-python-downloads", version])
    if completed is None or completed.returncode != 0:
        return ""
    found = [line.strip() for line in completed.stdout.strip().splitlines() if line.strip()]
    return found[-1] if found else ""


def available_pythons() -> list[tuple[str, str]]:
    """Return the supported versions already on this machine, newest first.

    Each entry is ``(version, interpreter path)``. An install prefers one of
    these over a download: the version is inside the matrix either way, but a
    CPython the machine already has needs no network and no surprise.
    """
    found: list[tuple[str, str]] = []
    for version in reversed(SUPPORTED_PYTHONS):
        named = shutil.which(f"python{version}")
        if named:
            found.append((version, named))
            continue
        managed = uv_python(version)
        if managed:
            found.append((version, managed))
    return found


def is_source_spec(source: str) -> bool:
    """Whether a source string is a package spec rather than a local path.

    ``install.sh`` takes either a checkout directory (the default) or something
    ``uv pip install`` understands directly, such as
    ``potluck @ git+https://host/repo@v0.1.0``. Only
    a path can be checked for existence, so the two have to be told apart.
    """
    return "://" in source or source.startswith("git+") or source.endswith(".whl")


@dataclass(frozen=True)
class Preflight:
    """The verdict of the pre-install compatibility check.

    Attributes:
        ok: Whether an install may proceed.
        python_version: The CPython version to build the virtualenv with.
        interpreter: A path for it on this machine, or ``""`` when ``uv`` will
            have to fetch one.
        host_target: This machine's matrix target id, or ``unsupported``.
        source: What will be installed.
        uv: The ``uv`` executable, or ``""`` when it is missing.
        problems: Why the check refused, one line per reason.
        guidance: What to do about it, one line per step.
        notes: True but worth saying (a download will happen, for example).
    """

    ok: bool
    python_version: str
    interpreter: str
    host_target: str
    source: str
    uv: str
    problems: tuple[str, ...]
    guidance: tuple[str, ...]
    notes: tuple[str, ...]

    def as_keys(self) -> dict[str, str]:
        """Return the ``SUPPORT_*`` keys a shell caller reads."""
        return {
            "STATUS": "ok" if self.ok else "refused",
            "HOST_TARGET": self.host_target,
            "HOST_SYSTEM": platform.system(),
            "HOST_MACHINE": platform.machine(),
            "PYTHON_VERSION": self.python_version,
            "INTERPRETER": self.interpreter,
            "SOURCE": self.source,
            "UV": self.uv,
        }


def preflight(*, python: str | None = None, source: str = "") -> Preflight:
    """Check this machine before an install changes anything.

    Three independent things are checked, and a refusal names all of them rather
    than stopping at the first, because a user who fixes only the first would
    otherwise have to run the installer again to learn about the second: the
    platform, the CPython version, and the tools a source install needs.

    Args:
        python: A version the caller was told to use (``--python``/AIPOTLUCK_PYTHON).
        source: What will be installed: a checkout path or a package spec.

    Returns:
        The verdict; ``ok`` is ``False`` whenever an install must not proceed.
    """
    problems: list[str] = []
    guidance: list[str] = []
    notes: list[str] = []

    row = host_target()
    host_id = row.target_id if row is not None else "unsupported"
    if row is None:
        problems.append(
            f"this machine ({platform.system()} {platform.machine()}) is not in the "
            "supported matrix"
        )
        guidance.append("supported first-batch environments:")
        guidance.extend(matrix_lines())

    if python is not None and not is_supported_python(python):
        problems.append(
            f"CPython {python} is outside the supported range "
            f"{SUPPORTED_PYTHONS[0]}-{SUPPORTED_PYTHONS[-1]}"
        )
        guidance.append("supported first-batch environments:")
        guidance.extend(matrix_lines())
        guidance.append(
            "with uv: `uv python install "
            f"{DEFAULT_PYTHON}`, then rerun the installer without --python"
        )

    available = available_pythons()
    version = python if python else (available[0][0] if available else DEFAULT_PYTHON)
    interpreter = ""
    for candidate_version, candidate_path in available:
        if candidate_version == version:
            interpreter = candidate_path
            break
    if not interpreter and (python is None or is_supported_python(python)):
        # Not a refusal: uv resolves the version itself and downloads a managed
        # build when this machine has none. It is said out loud because it turns
        # an install into a network fetch, and a surprise fetch is what makes an
        # install look hung.
        notes.append(
            f"CPython {version} is not on this machine yet, so uv will fetch a managed "
            "build (needs network)"
        )

    uv = uv_path()
    if not uv:
        problems.append("uv is not installed, and a source install needs it")
        guidance.append("install uv: curl -LsSf https://astral.sh/uv/install.sh | sh")
        guidance.append(
            "or do it by hand: "
            f"python{version} -m venv ~/.aipotluck/venv && "
            "~/.aipotluck/venv/bin/pip install <this checkout>"
        )

    if source and not is_source_spec(source) and not os.path.exists(source):
        problems.append(f"the source to install does not exist: {source}")
        guidance.append("run install.sh from inside a checkout, or pass a package spec")

    return Preflight(
        ok=not problems,
        python_version=version,
        interpreter=interpreter,
        host_target=host_id,
        source=source,
        uv=uv,
        problems=tuple(problems),
        guidance=tuple(guidance),
        notes=tuple(notes),
    )


def emit(keys: dict[str, str]) -> None:
    """Print ``SUPPORT_<KEY>=<value>`` lines a shell caller reads with ``sed``."""
    for key, value in keys.items():
        print(f"{KEY_PREFIX}{key}={value}")


def _report(text: str) -> None:
    """Print one human line on stderr."""
    print(text, file=sys.stderr)


def _cmd_preflight(args: argparse.Namespace) -> int:
    """Run the pre-install compatibility check."""
    verdict = preflight(python=args.python, source=args.source)
    emit(verdict.as_keys())
    for note in verdict.notes:
        _report(f"support_matrix: note: {note}")
    if not verdict.ok:
        for problem in verdict.problems:
            _report(f"support_matrix: refused: {problem}")
        for line in verdict.guidance:
            _report(f"  {line}")
        return 3
    where = f" at {verdict.interpreter}" if verdict.interpreter else " (uv will resolve it)"
    _report(
        f"support_matrix: preflight ok: host {verdict.host_target}, CPython "
        f"{verdict.python_version}{where}, source {verdict.source or '<this checkout>'}"
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    """Build the command line."""
    parser = argparse.ArgumentParser(
        prog="support_matrix.py",
        description="The potluck support matrix and install.sh's pre-install check.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("preflight", help="Run the pre-install compatibility check.")
    check.add_argument(
        "--python",
        default=None,
        help=f"A CPython version to require (default: newest supported here, "
        f"else {DEFAULT_PYTHON}).",
    )
    check.add_argument(
        "--source",
        default="",
        help="What will be installed: a checkout path or a package spec.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand."""
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "preflight":
        return _cmd_preflight(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
