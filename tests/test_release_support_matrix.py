"""The release support matrix and install.sh's pre-install refusal (F03/F04).

These are the offline, fast half of the release checks: the table that says which
machines and which CPython versions a release is built and tested for, the
refusals derived from it, and the two contracts a shell caller depends on — the
``SUPPORT_<KEY>=`` line protocol and "a refusal changes nothing". The end-to-end
half (installing for real and walking the default join/ask path) is
``scripts/release_gate.sh``, which is a script, not a test, because it needs
processes, ports and a real install.

Each test names the defect it guards: an unsupported machine that installs anyway
and fails inside uv, a version outside the matrix that is accepted because a
range check was loosened, or a refusal that has already created the prefix it was
supposed to leave alone.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

APP_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = APP_ROOT / "scripts"
SUPPORT_MATRIX = SCRIPTS / "support_matrix.py"


def _load_support_matrix() -> ModuleType:
    """Import scripts/support_matrix.py without touching sys.path.

    The module ships beside the installer rather than inside the package, because
    it has to run before a virtualenv exists; loading it by path keeps that
    placement from leaking into the test suite's import state.
    """
    spec = importlib.util.spec_from_file_location("support_matrix", SUPPORT_MATRIX)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registering before executing is not optional: the module's dataclasses use
    # `from __future__ import annotations`, and dataclasses resolves those
    # strings through sys.modules[cls.__module__].
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sm = _load_support_matrix()


def _run_cli(args: list[str], *, path: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run one support_matrix.py subcommand as a shell caller would."""
    env = dict(os.environ)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        [sys.executable, str(SUPPORT_MATRIX), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )


def _key(stdout: str, name: str) -> str:
    """Read one SUPPORT_<NAME> value out of the line protocol."""
    for line in stdout.splitlines():
        if line.startswith(f"SUPPORT_{name}="):
            return line.split("=", 1)[1]
    return ""


def _fake_uv(directory: Path, reply: str = "/fake/python3.13") -> Path:
    """Write an executable that stands in for uv's `python find`."""
    path = directory / "uv"
    path.write_text(f"#!/bin/sh\necho '{reply}'\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


# --------------------------------------------------------------------------
# the table itself
# --------------------------------------------------------------------------


def test_matrix_is_the_first_batch_and_nothing_more() -> None:
    """The matrix is exactly macOS arm64 plus Linux x86_64/arm64, CPython 3.11-3.13."""
    assert sm.supported_target_ids() == ("macos_arm64", "linux_x86_64", "linux_arm64")
    assert sm.SUPPORTED_PYTHONS == ("3.11", "3.12", "3.13")
    assert sm.DEFAULT_PYTHON == "3.13"


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "macos_arm64"),
        ("Linux", "x86_64", "linux_x86_64"),
        ("Linux", "amd64", "linux_x86_64"),
        ("Linux", "aarch64", "linux_arm64"),
        ("Linux", "arm64", "linux_arm64"),
        # Intel macOS and Windows are refused rather than "probably fine": no
        # build or gate ever ran on them, so a claim either way would be a guess.
        ("Darwin", "x86_64", None),
        ("Windows", "AMD64", None),
        ("FreeBSD", "amd64", None),
    ],
)
def test_host_target_maps_uname_onto_the_matrix(
    system: str, machine: str, expected: str | None
) -> None:
    """Host detection is a pure function of uname, so it can be pinned exactly."""
    target = sm.host_target(system=system, machine=machine)
    assert (target.target_id if target is not None else None) == expected


def test_matrix_listing_names_the_unsupported_combinations() -> None:
    """A refusal has to say what is *not* supported, not only what is."""
    listing = "\n".join(sm.matrix_lines())
    for target_id in sm.supported_target_ids():
        assert target_id in listing
    for note in ("Windows", "WSL", "Intel", "PyPy", "3.11", "3.13"):
        assert note in listing


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ("3.11", True),
        ("3.12", True),
        ("3.13", True),
        ("3.10", False),
        ("3.14", False),
        ("3.9", False),
        ("3", False),
        ("", False),
    ],
)
def test_python_versions_outside_the_range_are_refused(version: str, supported: bool) -> None:
    """The range check is a membership test, so 3.14 cannot slip through as "newer"."""
    assert sm.is_supported_python(version) is supported


@pytest.mark.parametrize(
    ("source", "is_spec"),
    [
        ("potluck @ git+https://host/repo@v0.1.0#subdirectory=apps/client", True),
        ("git+ssh://git@host/repo@main", True),
        ("https://host/potluck-0.1.0-py3-none-any.whl", True),
        ("/tmp/checkout/apps/client", False),
        (".", False),
    ],
)
def test_source_spec_is_told_apart_from_a_path(source: str, is_spec: bool) -> None:
    """Only a path can be checked for existence before an install starts."""
    assert sm.is_source_spec(source) is is_spec


# --------------------------------------------------------------------------
# the preflight verdict
# --------------------------------------------------------------------------


def test_preflight_refuses_a_machine_outside_the_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unsupported host is refused before anything is created, with the matrix printed."""
    monkeypatch.setattr(sm.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sm.platform, "machine", lambda: "AMD64")
    verdict = sm.preflight(python="3.13")
    assert verdict.ok is False
    assert verdict.host_target == "unsupported"
    assert any("not in the supported matrix" in problem for problem in verdict.problems)
    assert any("WSL" in line for line in verdict.guidance)


def test_preflight_refuses_a_python_outside_the_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--python 3.14` is refused even on a supported machine: it is untested, not just new."""
    monkeypatch.setattr(sm, "host_target", lambda **_: sm.target_of("macos_arm64"))
    verdict = sm.preflight(python="3.14")
    assert verdict.ok is False
    assert any("outside the supported range" in problem for problem in verdict.problems)
    # The refusal carries the matrix itself, so the next step is readable without
    # opening a README.
    assert any("3.11/3.12/3.13" in line for line in verdict.guidance)
    assert any("uv python install" in line for line in verdict.guidance)


def test_preflight_refuses_a_source_that_is_not_there(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing checkout is named before uv is asked to build from it."""
    monkeypatch.setattr(sm, "host_target", lambda **_: sm.target_of("linux_x86_64"))
    monkeypatch.setattr(sm, "uv_path", lambda: "/usr/bin/uv")
    missing = tmp_path / "no-such-checkout"
    verdict = sm.preflight(python="3.13", source=str(missing))
    assert verdict.ok is False
    assert any(str(missing) in problem for problem in verdict.problems)


def test_preflight_accepts_a_supported_machine_and_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The happy path reports the version and interpreter an install will use."""
    monkeypatch.setattr(sm, "host_target", lambda **_: sm.target_of("macos_arm64"))
    monkeypatch.setattr(sm, "uv_path", lambda: "/opt/bin/uv")
    monkeypatch.setattr(sm, "available_pythons", lambda: [("3.13", "/opt/bin/python3.13")])
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    verdict = sm.preflight(python="3.13", source=str(checkout))
    assert verdict.ok is True
    assert verdict.python_version == "3.13"
    assert verdict.interpreter == "/opt/bin/python3.13"
    assert verdict.uv == "/opt/bin/uv"
    assert verdict.as_keys()["STATUS"] == "ok"


def test_preflight_prefers_the_newest_supported_python_already_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no --python, an install uses what the machine has instead of downloading."""
    monkeypatch.setattr(sm, "host_target", lambda **_: sm.target_of("linux_arm64"))
    monkeypatch.setattr(sm, "uv_path", lambda: "/usr/bin/uv")
    monkeypatch.setattr(
        sm,
        "available_pythons",
        lambda: [("3.12", "/usr/bin/python3.12"), ("3.11", "/x/python3.11")],
    )
    verdict = sm.preflight()
    assert verdict.ok is True
    assert verdict.python_version == "3.12"
    assert verdict.interpreter == "/usr/bin/python3.12"
    assert verdict.notes == ()


def test_preflight_says_when_uv_will_have_to_fetch_a_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A network fetch is allowed, but it is announced rather than discovered as a hang."""
    monkeypatch.setattr(sm, "host_target", lambda **_: sm.target_of("linux_x86_64"))
    monkeypatch.setattr(sm, "uv_path", lambda: "/usr/bin/uv")
    monkeypatch.setattr(sm, "available_pythons", list)
    verdict = sm.preflight()
    assert verdict.ok is True
    assert verdict.python_version == sm.DEFAULT_PYTHON
    assert verdict.interpreter == ""
    assert any("fetch a managed build" in note for note in verdict.notes)


def test_preflight_refuses_when_uv_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A source install needs uv, so its absence is a refusal with the install line for it."""
    monkeypatch.setattr(sm, "host_target", lambda **_: sm.target_of("macos_arm64"))
    monkeypatch.setattr(sm, "uv_path", lambda: "")
    monkeypatch.setattr(sm, "available_pythons", lambda: [("3.13", "/opt/bin/python3.13")])
    verdict = sm.preflight()
    assert verdict.ok is False
    assert any("uv is not installed" in problem for problem in verdict.problems)
    assert any("astral.sh/uv" in line for line in verdict.guidance)


# --------------------------------------------------------------------------
# the line protocol a POSIX sh caller reads
# --------------------------------------------------------------------------


def test_cli_refuses_an_unsupported_python_with_the_line_protocol(tmp_path: Path) -> None:
    """install.sh reads SUPPORT_STATUS and the exit code; both have to say "no"."""
    completed = _run_cli(["preflight", "--python", "3.14", "--source", str(tmp_path)])
    assert completed.returncode == 3
    assert _key(completed.stdout, "STATUS") == "refused"
    assert "outside the supported range" in completed.stderr


def test_cli_reports_ok_for_a_supported_python(tmp_path: Path) -> None:
    """A supported version passes with the interpreter uv would build from."""
    if sm.host_target() is None:
        pytest.skip("this machine is outside the support matrix")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_uv(bin_dir)
    completed = _run_cli(
        ["preflight", "--python", "3.13", "--source", str(tmp_path)], path=str(bin_dir)
    )
    assert completed.returncode == 0
    assert _key(completed.stdout, "STATUS") == "ok"
    assert _key(completed.stdout, "PYTHON_VERSION") == "3.13"
    assert _key(completed.stdout, "INTERPRETER") == "/fake/python3.13"
    assert _key(completed.stdout, "UV") == str(bin_dir / "uv")


# --------------------------------------------------------------------------
# install.sh: refuse before changing anything
# --------------------------------------------------------------------------

INSTALL_SH = APP_ROOT / "install.sh"


def _run_install(args: list[str], prefix: Path) -> subprocess.CompletedProcess[str]:
    """Run install.sh with a prefix inside tmp_path, as a user would."""
    return subprocess.run(
        ["sh", str(INSTALL_SH), *args, "--prefix", str(prefix)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
def test_install_refuses_an_unsupported_python_before_touching_the_prefix(tmp_path: Path) -> None:
    """The F03 promise: a mismatch fails up front and leaves the machine as it was."""
    prefix = tmp_path / "potluck-home"
    completed = _run_install(["--preflight", "--python", "3.14"], prefix)
    assert completed.returncode == 1
    assert "refusing to install - nothing was changed" in completed.stderr
    assert "outside the supported range" in completed.stderr
    assert not prefix.exists(), "a refused preflight must not create the prefix"


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
def test_install_refuses_a_prefix_that_would_be_destructive(tmp_path: Path) -> None:
    """`--prefix /` is refused before the uninstall path could ever see it."""
    completed = subprocess.run(
        ["sh", str(INSTALL_SH), "--preflight", "--prefix", "/"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 1
    assert "refusing to use prefix" in completed.stderr


@pytest.mark.skipif(
    shutil.which("sh") is None or shutil.which("uv") is None,
    reason="needs a POSIX sh and uv",
)
def test_install_preflight_passes_on_a_supported_host(tmp_path: Path) -> None:
    """On a supported machine the same command exits 0 and still installs nothing."""
    if sm.host_target() is None:
        pytest.skip("this machine is outside the support matrix")
    prefix = tmp_path / "potluck-home"
    completed = _run_install(["--preflight"], prefix)
    assert completed.returncode == 0, completed.stderr
    assert "preflight passed; nothing was installed" in completed.stdout
    assert not prefix.exists()


# --------------------------------------------------------------------------
# the two READMEs quote the same matrix
# --------------------------------------------------------------------------


@pytest.mark.parametrize("readme", ["README.md", "README.zh-CN.md"])
def test_readme_states_the_supported_matrix(readme: str) -> None:
    """The documented matrix cannot drift from the one install.sh enforces."""
    text = (APP_ROOT / readme).read_text(encoding="utf-8")
    for target_id in sm.supported_target_ids():
        assert target_id in text, f"{readme} does not name {target_id}"
    for version in sm.SUPPORTED_PYTHONS:
        assert version in text, f"{readme} does not name CPython {version}"
    # The unsupported half is the part a reader acts on, so it is asserted too.
    for note in ("Windows", "WSL", "Intel", "PyPy"):
        assert note in text, f"{readme} does not say {note} is unsupported"
