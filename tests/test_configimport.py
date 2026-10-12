"""Importing the upstream a contributor's own tool is already configured with.

The readers are tested against files written the way Claude Code and Codex write
them, and the command is tested end to end through ``main()``: what lands in
``node.json``, in which mode, what is printed, and what is refused. The credential
is the reason this command exists, so every test that runs it asserts the key is
in the file and in no output.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any, cast

import pytest

from aipotluck.canonical import canonicalize, loads_strict
from aipotluck.cli import main
from aipotluck.configimport import (
    CLAUDE_CODE_SETTINGS,
    CODEX_CONFIG,
    ConfigImportError,
    ProviderStyle,
    completion_endpoint,
    merge_adapter,
    read_claude_code,
    read_codex_config,
    read_environment,
    resolve_upstream,
)
from aipotluck.deploy import load_node
from aipotluck.identity import generate_key_pair

UPSTREAM_KEY = "sk-upstream-do-not-print-me"
PLATFORM_TOKEN = "platform-token-do-not-print-me"
MODEL = "claude-sonnet-4-5"
BASE_URL = "https://relay.example/anthropic"
ENDPOINT = "https://relay.example/anthropic/v1/messages"


# --------------------------------------------------------------------------- #
# Sources, written the way their own tools write them
# --------------------------------------------------------------------------- #


def _write_claude_settings(path: Path, env: dict[str, Any]) -> Path:
    """Write a Claude Code ``settings.json`` carrying one ``env`` block."""
    path.write_text(
        json.dumps({"env": env, "permissions": {"allow": ["Bash(ls:*)"]}}),
        encoding="utf-8",
    )
    return path


def _write_codex_config(path: Path, text: str) -> Path:
    """Write a Codex ``config.toml``."""
    path.write_text(text, encoding="utf-8")
    return path


CODEX_TOML = """
model = "gpt-5-codex"
model_provider = "relay"

[model_providers.relay]
name = "Relay"
base_url = "https://relay.example/v1"
env_key = "RELAY_API_KEY"
wire_api = "chat"
"""


def _node_identity(directory: Path) -> Path:
    """Write the identity a ``join --node`` leaves behind, and return the directory."""
    public_key_b64, private_key_b64 = generate_key_pair()
    _device_public, device_private = generate_key_pair()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_bytes(
        canonicalize(
            {
                "center_url": "http://127.0.0.1:9",
                "key_id": "consumer-key-1",
                "subject_id": "consumer-1",
                "tenant_id": "tenant-1",
                "role": "node",
                "device_id": "device-1",
                "device_key_id": "device-key-1",
                "provider_id": "provider-1",
            }
        )
    )
    (directory / "key.json").write_bytes(
        canonicalize(
            {
                "public_key_b64": public_key_b64,
                "private_key_b64": private_key_b64,
                "device_private_key_b64": device_private,
            }
        )
    )
    return directory


def _existing_node_config(path: Path) -> dict[str, Any]:
    """Write a node configuration a contributor already has, and return it."""
    document: dict[str, Any] = {
        "center_url": "ws://127.0.0.1:9/v1/node-link",
        "node_id": "device-1",
        "device_id": "device-1",
        "device_key_id": "device-key-1",
        "provider_id": "provider-1",
        "database_path": str(path.with_name("node.db")),
        "token": PLATFORM_TOKEN,
        "device_private_key_b64": "ZGV2aWNlLWtleQ==",
        "adapter": {
            "kind": "http",
            "endpoint": "https://old.example/v1/chat/completions",
            "api_key": "an-old-key",
            "model": "an-old-model",
            "provider_style": "openai",
            "name": "main",
            "max_tokens": 2048,
        },
        "share_enabled": False,
        "max_concurrency": 3,
        "max_local_reserved_milli_credits": 5_000,
    }
    path.write_bytes(canonicalize(document))
    path.chmod(0o600)
    return document


# --------------------------------------------------------------------------- #
# Claude Code's settings
# --------------------------------------------------------------------------- #


def test_claude_code_settings_yield_an_anthropic_upstream(tmp_path: Path) -> None:
    """A tool's base URL becomes the full URL this node's adapter POSTs to.

    Completing the path is the part an import has to get right: an endpoint that
    merely looked correct would fail every attempt the center dispatched here.
    """
    settings = _write_claude_settings(
        tmp_path / "settings.json",
        {
            "ANTHROPIC_BASE_URL": BASE_URL,
            "ANTHROPIC_AUTH_TOKEN": UPSTREAM_KEY,
            "ANTHROPIC_MODEL": MODEL,
        },
    )

    partial = read_claude_code(settings)
    upstream = resolve_upstream(partial)

    assert partial.gaps() == ()
    assert upstream.endpoint == ENDPOINT
    assert upstream.api_key == UPSTREAM_KEY
    assert upstream.model == MODEL
    assert upstream.provider_style == "anthropic"
    assert str(settings) in upstream.source
    assert UPSTREAM_KEY not in upstream.summary(), "the summary is the safe rendering"


def test_the_second_credential_spelling_is_read_when_the_first_is_absent(
    tmp_path: Path,
) -> None:
    """Claude Code sends one as a bearer header and the other as ``x-api-key``."""
    settings = _write_claude_settings(
        tmp_path / "settings.json",
        {
            "ANTHROPIC_BASE_URL": BASE_URL,
            "ANTHROPIC_API_KEY": UPSTREAM_KEY,
            "ANTHROPIC_MODEL": MODEL,
        },
    )

    assert resolve_upstream(read_claude_code(settings)).api_key == UPSTREAM_KEY


@pytest.mark.parametrize(
    ("base_url", "style", "expected"),
    [
        (
            "https://relay.example/anthropic",
            "anthropic",
            "https://relay.example/anthropic/v1/messages",
        ),
        ("https://relay.example/v1", "anthropic", "https://relay.example/v1/messages"),
        (
            "https://relay.example/v1/messages",
            "anthropic",
            "https://relay.example/v1/messages",
        ),
        ("https://relay.example/v1/", "anthropic", "https://relay.example/v1/messages"),
        (
            "https://relay.example/v1",
            "openai",
            "https://relay.example/v1/chat/completions",
        ),
        (
            "https://relay.example/v1/chat/completions",
            "openai",
            "https://relay.example/v1/chat/completions",
        ),
    ],
)
def test_a_base_url_is_completed_once_and_never_twice(
    base_url: str, style: str, expected: str
) -> None:
    """`/v1/v1/messages` in a node configuration is a configuration that cannot work."""
    provider_style = cast("ProviderStyle", style)

    assert completion_endpoint(base_url, provider_style) == expected


def test_the_legacy_completions_endpoint_is_refused_rather_than_completed() -> None:
    """This node posts a chat-completions body, so a base URL naming another
    endpoint is a configuration to ask about, not to guess at.
    """
    with pytest.raises(ConfigImportError, match="legacy completions"):
        completion_endpoint("https://relay.example/v1/completions", "openai")


def test_a_relative_base_url_is_refused() -> None:
    """An endpoint this node could not POST to is refused at import time."""
    with pytest.raises(ConfigImportError, match="absolute URL"):
        completion_endpoint("relay.example/anthropic", "anthropic")


def test_every_field_a_source_does_not_name_is_reported_at_once(tmp_path: Path) -> None:
    """One run names every gap, each with the key path and the flag that fills it.

    Reporting them one per run would make a contributor edit their own file three
    times to learn what one message could have said.
    """
    settings = _write_claude_settings(
        tmp_path / "settings.json", {"ANTHROPIC_BASE_URL": BASE_URL}
    )

    partial = read_claude_code(settings)
    assert [gap.field for gap in partial.gaps()] == ["api_key", "model"]
    with pytest.raises(ConfigImportError) as raised:
        resolve_upstream(partial)

    message = str(raised.value)
    assert "no node configuration was written" in message
    assert "ANTHROPIC_AUTH_TOKEN" in message and "ANTHROPIC_API_KEY" in message
    assert "ANTHROPIC_MODEL" in message
    assert "--upstream-key-file" in message and "--upstream-model" in message
    assert str(settings) in message


def test_an_explicit_argument_fills_the_gap_a_source_left(tmp_path: Path) -> None:
    """A contributor supplies what their tool's file does not name, without editing it."""
    settings = _write_claude_settings(
        tmp_path / "settings.json",
        {"ANTHROPIC_BASE_URL": BASE_URL, "ANTHROPIC_AUTH_TOKEN": UPSTREAM_KEY},
    )

    upstream = resolve_upstream(read_claude_code(settings), model=MODEL)

    assert upstream.model == MODEL
    assert upstream.endpoint == ENDPOINT


def test_an_explicit_endpoint_outranks_the_imported_one(tmp_path: Path) -> None:
    """The file belongs to another program; the instruction belongs to the operator."""
    settings = _write_claude_settings(
        tmp_path / "settings.json",
        {
            "ANTHROPIC_BASE_URL": BASE_URL,
            "ANTHROPIC_AUTH_TOKEN": UPSTREAM_KEY,
            "ANTHROPIC_MODEL": MODEL,
        },
    )

    upstream = resolve_upstream(
        read_claude_code(settings), endpoint="https://other.example/v1/messages"
    )

    assert upstream.endpoint == "https://other.example/v1/messages"


def test_a_field_of_the_wrong_type_is_a_malformed_source_not_an_absent_one(
    tmp_path: Path,
) -> None:
    """``"model": 3`` is a typo somebody needs to see, not a gap to fill silently."""
    settings = _write_claude_settings(
        tmp_path / "settings.json",
        {"ANTHROPIC_BASE_URL": BASE_URL, "ANTHROPIC_AUTH_TOKEN": UPSTREAM_KEY,
         "ANTHROPIC_MODEL": 3},
    )

    with pytest.raises(ConfigImportError, match=r"ANTHROPIC_MODEL.*must be a string"):
        read_claude_code(settings)


def test_an_empty_credential_is_refused_rather_than_imported(tmp_path: Path) -> None:
    """An empty key would configure a node whose every upstream call is refused."""
    settings = _write_claude_settings(
        tmp_path / "settings.json",
        {"ANTHROPIC_BASE_URL": BASE_URL, "ANTHROPIC_AUTH_TOKEN": "   ",
         "ANTHROPIC_MODEL": MODEL},
    )

    with pytest.raises(ConfigImportError, match="is empty"):
        read_claude_code(settings)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{not json", "not valid JSON"),
        ("[1, 2]", "must hold a JSON object"),
        ('{"env": "not-an-object"}', "'env' in .* must be an object"),
    ],
)
def test_a_settings_file_this_command_cannot_read_is_refused_by_name(
    tmp_path: Path, text: str, expected: str
) -> None:
    """A malformed source is named, so the failure points at the file and not here."""
    path = tmp_path / "settings.json"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigImportError, match=expected):
        read_claude_code(path)


def test_an_absent_source_file_is_refused_rather_than_read_as_empty(
    tmp_path: Path,
) -> None:
    """Silently importing nothing would write a node with no upstream at all."""
    with pytest.raises(ConfigImportError, match="could not be read"):
        read_claude_code(tmp_path / "absent.json")


# --------------------------------------------------------------------------- #
# Codex's configuration
# --------------------------------------------------------------------------- #


def test_codex_config_yields_an_openai_upstream(tmp_path: Path) -> None:
    """The credential is read from the variable ``env_key`` names, where Codex reads it."""
    config = _write_codex_config(tmp_path / "config.toml", CODEX_TOML)

    upstream = resolve_upstream(
        read_codex_config(config, {"RELAY_API_KEY": UPSTREAM_KEY})
    )

    assert upstream.endpoint == "https://relay.example/v1/chat/completions"
    assert upstream.model == "gpt-5-codex"
    assert upstream.api_key == UPSTREAM_KEY
    assert upstream.provider_style == "openai"


def test_an_unset_credential_variable_is_reported_by_the_name_codex_gave_it(
    tmp_path: Path,
) -> None:
    """The gap names the variable, so the remedy is to export it, not to guess."""
    config = _write_codex_config(tmp_path / "config.toml", CODEX_TOML)

    with pytest.raises(ConfigImportError) as raised:
        resolve_upstream(read_codex_config(config, {}))

    message = str(raised.value)
    assert "RELAY_API_KEY" in message
    assert "model_providers.relay.env_key" in message


def test_a_provider_codex_itself_could_not_resolve_is_refused(tmp_path: Path) -> None:
    """Naming what the file defines is what makes the answer actionable."""
    config = _write_codex_config(
        tmp_path / "config.toml",
        CODEX_TOML.replace('model_provider = "relay"', 'model_provider = "absent"'),
    )

    with pytest.raises(ConfigImportError, match=r"defines \['relay'\]"):
        read_codex_config(config, {"RELAY_API_KEY": UPSTREAM_KEY})


def test_a_wire_api_this_adapter_cannot_speak_is_refused(tmp_path: Path) -> None:
    """A node posting a chat-completions body at a responses-only upstream would
    fail every attempt, and the failure would surface at the upstream, not here.
    """
    config = _write_codex_config(
        tmp_path / "config.toml",
        CODEX_TOML.replace('wire_api = "chat"', 'wire_api = "responses"'),
    )

    with pytest.raises(ConfigImportError, match="wire_api"):
        read_codex_config(config, {"RELAY_API_KEY": UPSTREAM_KEY})


def test_a_config_with_no_provider_block_reports_both_gaps(tmp_path: Path) -> None:
    """A plain ``model`` and nothing else is a real Codex configuration, and it is
    not one this import can complete.
    """
    config = _write_codex_config(tmp_path / "config.toml", 'model = "gpt-5-codex"\n')

    partial = read_codex_config(config, {})
    assert [gap.field for gap in partial.gaps()] == ["endpoint", "api_key"]
    with pytest.raises(ConfigImportError) as raised:
        resolve_upstream(partial)

    assert "model_provider" in str(raised.value)


def test_invalid_toml_is_refused_by_name(tmp_path: Path) -> None:
    config = _write_codex_config(tmp_path / "config.toml", "model = \n")

    with pytest.raises(ConfigImportError, match="not valid TOML"):
        read_codex_config(config, {})


# --------------------------------------------------------------------------- #
# The environment
# --------------------------------------------------------------------------- #


def test_the_openai_family_decides_the_style_and_the_endpoint() -> None:
    environ = {
        "OPENAI_BASE_URL": "https://api.example/v1",
        "OPENAI_API_KEY": UPSTREAM_KEY,
        "OPENAI_MODEL": "gpt-4o-mini",
    }

    upstream = resolve_upstream(read_environment(environ))

    assert upstream.endpoint == "https://api.example/v1/chat/completions"
    assert upstream.provider_style == "openai"
    assert upstream.model == "gpt-4o-mini"


def test_the_anthropic_family_is_read_when_it_is_the_one_that_is_set() -> None:
    environ = {
        "ANTHROPIC_BASE_URL": BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": UPSTREAM_KEY,
        "ANTHROPIC_MODEL": MODEL,
    }

    upstream = resolve_upstream(read_environment(environ))

    assert upstream.endpoint == ENDPOINT
    assert upstream.provider_style == "anthropic"


def test_a_partially_set_family_reports_its_own_gaps() -> None:
    """A base URL with no credential is a shell half-configured, and says which half."""
    partial = read_environment({"OPENAI_BASE_URL": "https://api.example/v1"})

    assert [gap.field for gap in partial.gaps()] == ["api_key", "model"]
    with pytest.raises(ConfigImportError, match="OPENAI_API_KEY"):
        resolve_upstream(partial)


def test_an_environment_that_names_no_family_lists_every_variable_it_looked_for() -> None:
    """"Nothing was set" is not an actionable answer; the names are."""
    with pytest.raises(ConfigImportError) as raised:
        resolve_upstream(read_environment({}))

    message = str(raised.value)
    for name in ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY",
                 "ANTHROPIC_AUTH_TOKEN", "OPENAI_MODEL", "ANTHROPIC_MODEL"):
        assert name in message


# --------------------------------------------------------------------------- #
# Merging into a node configuration
# --------------------------------------------------------------------------- #


def test_merging_replaces_the_adapter_and_keeps_what_belongs_to_the_node() -> None:
    """The token, the device binding, the limits and the share switch are not an
    upstream's business, and an import that reset them would silently undo a
    contributor's own choices."""
    document = {
        "token": PLATFORM_TOKEN,
        "device_id": "device-1",
        "share_enabled": False,
        "max_concurrency": 3,
        "adapter": {"kind": "fake", "model": "old", "name": "main", "max_tokens": 2048},
    }
    upstream = resolve_upstream(
        read_environment(
            {
                "OPENAI_BASE_URL": "https://api.example/v1",
                "OPENAI_API_KEY": UPSTREAM_KEY,
                "OPENAI_MODEL": "gpt-4o-mini",
            }
        )
    )

    merged = merge_adapter(document, upstream)

    assert merged["token"] == PLATFORM_TOKEN
    assert merged["share_enabled"] is False
    assert merged["max_concurrency"] == 3
    assert merged["adapter"] == {
        "kind": "http",
        "endpoint": "https://api.example/v1/chat/completions",
        "api_key": UPSTREAM_KEY,
        "model": "gpt-4o-mini",
        "provider_style": "openai",
        "name": "main",
        "max_tokens": 2048,
    }
    assert document["adapter"]["kind"] == "fake", "the argument is not modified"


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def _import_argv(source: list[str], directory: Path, **extra: str) -> list[str]:
    """Build one ``contribute import-config`` invocation."""
    argv = ["contribute", "import-config", *source, "--config-dir", str(directory)]
    for name, value in extra.items():
        argv += [f"--{name.replace('_', '-')}", value]
    return argv


def test_import_creates_a_node_configuration_the_platform_can_load(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The imported upstream, the joined identity's device binding, and mode 0600.

    Loaded back through `load_node` rather than only read as JSON: a configuration
    this command writes and the connector refuses to start from would be a
    configuration that looks right and is not.
    """
    directory = _node_identity(tmp_path / "aipotluck")
    token_file = tmp_path / "token.txt"
    token_file.write_text(PLATFORM_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    settings = _write_claude_settings(
        tmp_path / "settings.json",
        {
            "ANTHROPIC_BASE_URL": BASE_URL,
            "ANTHROPIC_AUTH_TOKEN": UPSTREAM_KEY,
            "ANTHROPIC_MODEL": MODEL,
        },
    )
    monkeypatch.delenv("AIPOTLUCK_UPSTREAM_KEY", raising=False)
    node_path = directory / "node.json"

    code = main(
        _import_argv(
            ["--from-claude-code", str(settings)],
            directory,
            node_token_file=str(token_file),
        )
    )
    captured = capsys.readouterr()

    assert code == 0
    document = loads_strict(node_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    assert document["adapter"] == {
        "kind": "http",
        "endpoint": ENDPOINT,
        "api_key": UPSTREAM_KEY,
        "model": MODEL,
        "provider_style": "anthropic",
        "name": "main",
    }
    assert document["token"] == PLATFORM_TOKEN
    assert document["device_id"] == "device-1"
    assert stat.S_IMODE(node_path.stat().st_mode) == 0o600
    config, adapter = load_node(node_path)
    assert config.model == MODEL
    assert adapter.endpoint == ENDPOINT
    assert UPSTREAM_KEY not in captured.out + captured.err
    assert PLATFORM_TOKEN not in captured.out + captured.err
    assert ENDPOINT in captured.out, "the endpoint is configuration, so it is reported"
    assert "not printed" in captured.out


def test_import_updates_an_existing_node_configuration_in_place(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the adapter block moves; a paused node stays paused."""
    directory = tmp_path / "aipotluck"
    directory.mkdir()
    node_path = directory / "node.json"
    before = _existing_node_config(node_path)
    key_file = tmp_path / "upstream.key"
    key_file.write_text(f"{UPSTREAM_KEY}\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AIPOTLUCK_UPSTREAM_KEY", raising=False)

    code = main(
        _import_argv(
            ["--from-env"],
            directory,
            upstream_key_file=str(key_file),
            config=str(node_path),
        )
    )
    captured = capsys.readouterr()

    assert code == 0
    after = loads_strict(node_path.read_text(encoding="utf-8"))
    assert isinstance(after, dict)
    assert after["token"] == before["token"]
    assert after["device_id"] == before["device_id"]
    assert after["share_enabled"] is False, "a paused node is not resumed by an import"
    assert after["max_concurrency"] == before["max_concurrency"]
    assert after["adapter"]["max_tokens"] == 2048
    assert after["adapter"]["api_key"] == UPSTREAM_KEY
    assert after["adapter"]["endpoint"] == "https://api.example/v1/chat/completions"
    assert stat.S_IMODE(node_path.stat().st_mode) == 0o600
    assert UPSTREAM_KEY not in captured.out + captured.err
    assert PLATFORM_TOKEN not in captured.out + captured.err


def test_an_import_from_the_environment_reads_the_variables_it_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--from-env`` is the path a contributor with an exported key already has."""
    directory = _node_identity(tmp_path / "aipotluck")
    token_file = tmp_path / "token.txt"
    token_file.write_text(PLATFORM_TOKEN, encoding="utf-8")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", UPSTREAM_KEY)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    monkeypatch.delenv("AIPOTLUCK_UPSTREAM_KEY", raising=False)

    code = main(
        _import_argv(["--from-env"], directory, node_token_file=str(token_file))
    )
    captured = capsys.readouterr()

    assert code == 0
    document = loads_strict((directory / "node.json").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    assert document["adapter"]["endpoint"] == "https://api.example/v1/chat/completions"
    assert document["adapter"]["api_key"] == UPSTREAM_KEY
    assert document["adapter"]["provider_style"] == "openai"
    assert UPSTREAM_KEY not in captured.out + captured.err


def test_an_incomplete_import_writes_nothing_and_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused import leaves the node exactly as it was, and says what is missing."""
    directory = tmp_path / "aipotluck"
    directory.mkdir()
    node_path = directory / "node.json"
    before = _existing_node_config(node_path)
    written = node_path.read_bytes()
    settings = _write_claude_settings(
        tmp_path / "settings.json", {"ANTHROPIC_BASE_URL": BASE_URL}
    )
    monkeypatch.delenv("AIPOTLUCK_UPSTREAM_KEY", raising=False)

    code = main(_import_argv(["--from-claude-code", str(settings)], directory))
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert "ANTHROPIC_MODEL" in captured.err
    assert "no node configuration was written" in captured.err
    assert node_path.read_bytes() == written
    assert loads_strict(node_path.read_text(encoding="utf-8")) == before


def test_an_import_without_a_source_is_refused_by_the_parser(tmp_path: Path) -> None:
    """Three sources, exactly one per run, and none is a default."""
    with pytest.raises(SystemExit) as raised:
        main(["contribute", "import-config", "--config-dir", str(tmp_path)])

    assert raised.value.code == 2


def test_two_sources_at_once_are_refused_by_the_parser(tmp_path: Path) -> None:
    """A run that reads two files would have to invent a precedence rule."""
    with pytest.raises(SystemExit) as raised:
        main(
            [
                "contribute",
                "import-config",
                "--from-env",
                "--from-claude-code",
                str(tmp_path / "settings.json"),
                "--config-dir",
                str(tmp_path),
            ]
        )

    assert raised.value.code == 2


def test_the_documented_default_paths_are_the_ones_the_tools_use() -> None:
    """The defaults are a claim about another program's file layout, so they are pinned."""
    assert str(CLAUDE_CODE_SETTINGS) == "~/.claude/settings.json"
    assert str(CODEX_CONFIG) == "~/.codex/config.toml"
