"""The configuration a tool is handed for the loopback endpoint.

The blocks are asserted as exact text rather than as "contains a base URL",
because the two families differ by one path segment and both are wrong silently:
an OpenAI client appends ``chat/completions`` to whatever base URL it is given,
while Claude Code appends ``/v1/messages`` itself. A snapshot is what pins the
difference down.

The last test in ``tests/test_localapi.py`` closes the loop the other way: it
emits a block for a running endpoint and makes the request that block describes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from aipotluck.cli import main
from aipotluck.configemit import (
    ANTHROPIC_BASE_URL_ENV,
    ANTHROPIC_MODEL_ENV,
    ANTHROPIC_SMALL_FAST_MODEL_ENV,
    OPENAI_API_KEY_ENV,
    OPENAI_BASE_URL_ENV,
    claude_code_guidance,
    render_claude_code,
    render_shell,
    shell_guidance,
    target_for,
)
from aipotluck.localapi import LOCAL_API_KEY_PLACEHOLDER, NonLoopbackBindError

MODEL = "coding-pool"


def test_the_shell_block_carries_the_v1_prefix_an_openai_client_needs() -> None:
    """Snapshot: the block an operator pastes, comments and all.

    The comments are part of the output because a pasted ``OPENAI_API_KEY`` with
    no note beside it reads as a credential somebody should protect, and this one
    is a placeholder for an endpoint that checks no key at all.
    """
    block = render_shell(target_for(port=8787))

    assert block == (
        "# potluck serve-openai on 127.0.0.1:8787 — loopback only, and\n"
        "# it checks no API key: the value below satisfies a client that requires\n"
        "# one, and is not a secret.\n"
        'export OPENAI_BASE_URL="http://127.0.0.1:8787/v1"\n'
        f'export OPENAI_API_KEY="{LOCAL_API_KEY_PLACEHOLDER}"\n'
        "# The models served are whatever the pool's connected nodes advertise:\n"
        '#   curl -s "$OPENAI_BASE_URL/models"\n'
        "# Pass a model to the tool as well; a base URL does not choose one, and\n"
        "# a name the pool does not serve is refused with the models it does.\n"
    )
    assert OPENAI_BASE_URL_ENV in block and OPENAI_API_KEY_ENV in block


def test_a_named_model_is_advised_for_a_shell_rather_than_exported_as_an_invention() -> None:
    """No standard variable carries a model to an OpenAI client, so none is invented.

    A made-up ``OPENAI_MODEL`` would look authoritative in a shell profile and be
    read by nothing, which is worse than a comment naming the model.
    """
    block = render_shell(target_for(port=8787, model=MODEL))

    assert f"# Pass the model to the tool as well; a base URL does not choose one: {MODEL}" in block
    assert "OPENAI_MODEL" not in block


def test_the_claude_code_block_points_at_the_root_because_the_tool_appends_the_route() -> None:
    """Snapshot: the ``env`` block of Claude Code's ``settings.json``.

    ``ANTHROPIC_BASE_URL`` carries no ``/v1`` — Claude Code appends
    ``/v1/messages`` itself, which is the route this endpoint serves. Both the
    small/fast model and the main one are named, because Claude Code's background
    calls use the second and a name the pool does not serve would fail them in a
    place the operator is not looking.
    """
    document = render_claude_code(target_for(port=8787, model=MODEL))

    assert document == {
        "env": {
            ANTHROPIC_BASE_URL_ENV: "http://127.0.0.1:8787",
            "ANTHROPIC_AUTH_TOKEN": LOCAL_API_KEY_PLACEHOLDER,
            ANTHROPIC_MODEL_ENV: MODEL,
            ANTHROPIC_SMALL_FAST_MODEL_ENV: MODEL,
        }
    }
    target = target_for(port=8787, model=MODEL)
    assert target.messages_url == "http://127.0.0.1:8787/v1/messages"


def test_a_model_nobody_named_is_warned_about_rather_than_invented() -> None:
    """Claude Code will send its own default name, which this pool may not serve."""
    target = target_for(port=8787)

    assert ANTHROPIC_MODEL_ENV not in render_claude_code(target)["env"]
    guidance = " ".join(claude_code_guidance(target))
    assert "no --model given" in guidance
    assert "curl -s http://127.0.0.1:8787/v1/models" in guidance


def test_the_guidance_says_where_the_block_goes_and_what_the_token_is() -> None:
    """The document on stdout is JSON and cannot carry a comment, so this is where
    an operator is told the block is merged rather than written over a settings
    file, and that the token is a placeholder.
    """
    claude_guidance = " ".join(claude_code_guidance(target_for(model=MODEL)))
    shell = " ".join(shell_guidance(target_for()))

    assert "~/.claude/settings.json" in claude_guidance
    assert "placeholder" in claude_guidance
    assert "serve-openai" in claude_guidance
    assert "serve-openai" in shell
    assert LOCAL_API_KEY_PLACEHOLDER not in claude_guidance, (
        "the placeholder is emitted, not explained as a secret"
    )


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "::", "::1", "8.8.8.8", "localhost", "example.com", ""]
)
def test_the_emitter_refuses_an_address_the_endpoint_would_not_bind(host: str) -> None:
    """Emitting a configuration for an address that cannot be served would emit one
    that cannot work, so the endpoint's own check is reused rather than restated.
    """
    with pytest.raises(NonLoopbackBindError):
        target_for(host=host, port=8787)


def test_the_shell_block_is_printed_on_stdout_and_the_guidance_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """So the block can be evaluated by a shell without the guidance in it."""
    code = main(["config", "emit", "--shell", "--port", "9001"])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out == render_shell(target_for(port=9001))
    assert captured.err.startswith("potluck: ")
    assert "export" not in captured.err


def test_the_claude_code_block_is_printed_as_one_json_document(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Machine-readable on stdout, so it can be piped into a merge of settings.json."""
    code = main(["config", "emit", "--claude-code", "--port", "9001", "--model", MODEL])
    captured = capsys.readouterr()

    assert code == 0
    document: Any = json.loads(captured.out)
    assert document == render_claude_code(target_for(port=9001, model=MODEL))
    assert captured.out.count("\n") == 1, "one document, one line"
    assert captured.err and "{" not in captured.err


def test_a_host_the_endpoint_refuses_is_refused_here_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The refusal is the endpoint's own wording, so the two cannot drift."""
    code = main(["config", "emit", "--shell", "--host", "10.0.0.4"])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert "loopback" in captured.err


@pytest.mark.parametrize("argv", [["config", "emit"], ["config"]])
def test_a_shape_and_a_subcommand_are_required(argv: list[str]) -> None:
    """Emitting both shapes at once would be two documents on one stdout."""
    with pytest.raises(SystemExit) as raised:
        main(argv)

    assert raised.value.code == 2
