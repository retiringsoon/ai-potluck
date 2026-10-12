"""Emit the configuration a tool needs to talk to this machine's endpoint.

Why this exists
---------------
``potluck serve-openai``'s claim is that a tool needs a base URL and no
potluck-specific code, and the last mile of that claim is the block an operator
pastes. Written by hand it is where two mistakes land, and both are one character
to make and silent to debug — the tool reports a ``404`` from an endpoint that is
running:

* ``OPENAI_BASE_URL`` has to carry ``/v1``, because an OpenAI client appends
  ``chat/completions`` to whatever it is given.
* ``ANTHROPIC_BASE_URL`` has to *not* carry it, because Claude Code appends
  ``/v1/messages`` itself.

So the two are rendered from one definition of the endpoint's addresses, next to
the endpoint that serves them, and the api_key both carry is
:data:`aipotluck.localapi.LOCAL_API_KEY_PLACEHOLDER` — a value that names itself,
because a client that cannot send a non-empty credential will not talk at all and
a placeholder that looked like a secret would be a credential nobody can rotate.

Two shapes, because two families read configuration differently: ``--shell``
renders ``export`` lines for a shell profile, and ``--claude-code`` renders the
``env`` block of Claude Code's ``settings.json`` as one JSON document on stdout,
so it can be piped into a merge rather than retyped.

Secrecy: nothing here reads a credential, a key file, or the environment. The
only value it emits that occupies a credential's slot is the documented
placeholder, so no output of this module can carry a secret.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from .configimport import CLAUDE_CODE_SETTINGS
from .localapi import DEFAULT_PORT, LOCAL_API_KEY_PLACEHOLDER, LOOPBACK_HOST, require_loopback

#: The environment variables an OpenAI-compatible tool reads.
OPENAI_BASE_URL_ENV: Final = "OPENAI_BASE_URL"
OPENAI_API_KEY_ENV: Final = "OPENAI_API_KEY"

#: The environment variables Claude Code reads. The base URL is the endpoint's
#: root: Claude Code appends ``/v1/messages`` itself.
ANTHROPIC_BASE_URL_ENV: Final = "ANTHROPIC_BASE_URL"
ANTHROPIC_AUTH_TOKEN_ENV: Final = "ANTHROPIC_AUTH_TOKEN"
ANTHROPIC_MODEL_ENV: Final = "ANTHROPIC_MODEL"
#: Claude Code's background calls use a second, smaller model name. Pointed at the
#: same model as the main one, because a name this pool does not serve would fail
#: every one of those calls in a place the operator is not looking.
ANTHROPIC_SMALL_FAST_MODEL_ENV: Final = "ANTHROPIC_SMALL_FAST_MODEL"


@dataclass(frozen=True)
class LocalEndpointTarget:
    """Where one local endpoint listens, and the model a tool should name.

    Attributes:
        host: The endpoint's bind address, which `target_for` refuses to let be
            anything but IPv4 loopback.
        port: The endpoint's port.
        model: The model to configure a tool with, or `None` to leave the tool's
            own default alone — which the guidance then warns about, since a
            default model name is a name this pool may not serve.
    """

    host: str = LOOPBACK_HOST
    port: int = DEFAULT_PORT
    model: str | None = None

    @property
    def openai_base_url(self) -> str:
        """Return the base URL an OpenAI-compatible tool is given."""
        return f"http://{self.host}:{self.port}/v1"

    @property
    def anthropic_base_url(self) -> str:
        """Return the base URL an Anthropic-shaped tool is given, without ``/v1``."""
        return f"http://{self.host}:{self.port}"

    @property
    def messages_url(self) -> str:
        """Return the full URL Claude Code's requests will land on."""
        return f"{self.anthropic_base_url}/v1/messages"

    @property
    def models_url(self) -> str:
        """Return the URL that lists what the pool can serve right now."""
        return f"{self.openai_base_url}/models"


def target_for(
    *, host: str = LOOPBACK_HOST, port: int = DEFAULT_PORT, model: str | None = None
) -> LocalEndpointTarget:
    """Return the endpoint a tool should be pointed at.

    Args:
        host: The endpoint's address.
        port: The endpoint's port.
        model: The model to configure a tool with, if one was named.

    Returns:
        The target.

    Raises:
        NonLoopbackBindError: If `host` is not an IPv4 loopback address. The
            endpoint's own check is reused rather than restated: emitting a
            configuration that points a tool at an address the endpoint refuses to
            bind would be emitting a configuration that cannot work.
    """
    return LocalEndpointTarget(host=require_loopback(host), port=port, model=model)


def render_shell(target: LocalEndpointTarget) -> str:
    """Render the ``export`` lines for an OpenAI-compatible tool.

    Args:
        target: The endpoint to point a tool at.

    Returns:
        The block, comments included, ending in a newline. The comments are part
        of the output rather than printed separately because they are what makes
        the placeholder key legible: pasted without them, an exported
        ``OPENAI_API_KEY`` reads as a credential somebody should protect.
    """
    lines = [
        f"# potluck serve-openai on {target.host}:{target.port} — loopback only, and",
        "# it checks no API key: the value below satisfies a client that requires",
        "# one, and is not a secret.",
        f'export {OPENAI_BASE_URL_ENV}="{target.openai_base_url}"',
        f'export {OPENAI_API_KEY_ENV}="{LOCAL_API_KEY_PLACEHOLDER}"',
        "# The models served are whatever the pool's connected nodes advertise:",
        f'#   curl -s "${OPENAI_BASE_URL_ENV}/models"',
    ]
    if target.model is not None:
        lines.append(
            "# Pass the model to the tool as well; a base URL does not choose one:"
            f" {target.model}"
        )
    else:
        lines += [
            "# Pass a model to the tool as well; a base URL does not choose one, and",
            "# a name the pool does not serve is refused with the models it does.",
        ]
    return "\n".join(lines) + "\n"


def render_claude_code(target: LocalEndpointTarget) -> dict[str, Any]:
    """Render the ``env`` block of Claude Code's ``settings.json``.

    Args:
        target: The endpoint to point Claude Code at.

    Returns:
        One document with a single ``env`` key, ready to be merged into the
        settings file. ``ANTHROPIC_MODEL`` is emitted only when a model was named:
        inventing one would configure a tool with a model the pool may not serve,
        which is worse than leaving its own default in place and warning about it.
    """
    env: dict[str, str] = {
        ANTHROPIC_BASE_URL_ENV: target.anthropic_base_url,
        ANTHROPIC_AUTH_TOKEN_ENV: LOCAL_API_KEY_PLACEHOLDER,
    }
    if target.model is not None:
        env[ANTHROPIC_MODEL_ENV] = target.model
        env[ANTHROPIC_SMALL_FAST_MODEL_ENV] = target.model
    return {"env": env}


def shell_guidance(target: LocalEndpointTarget) -> tuple[str, ...]:
    """Return the lines that go to stderr beside the shell block."""
    return (
        "evaluate the block above in the shell the tool runs in, or paste it into "
        "that shell's profile",
        f"start the endpoint with `potluck serve-openai --port {target.port}`; it "
        f"signs each request with this machine's joined identity, which never "
        "leaves it",
    )


def claude_code_guidance(target: LocalEndpointTarget) -> tuple[str, ...]:
    """Return the lines that go to stderr beside the Claude Code block.

    The document on stdout is JSON, so it cannot carry a comment; everything an
    operator has to know to use it — where it goes, that the token is a
    placeholder, and what to do about a model — is said here instead.
    """
    lines = [
        f"merge the env block above into {CLAUDE_CODE_SETTINGS} (Claude Code reads "
        "it at start-up); it points "
        f"{ANTHROPIC_BASE_URL_ENV} at {target.anthropic_base_url}, and Claude Code "
        f"appends /v1/messages itself, which is what {target.messages_url} serves",
        f"the {ANTHROPIC_AUTH_TOKEN_ENV} value is a placeholder: the endpoint checks "
        "no key and signs with this machine's joined identity instead",
        f"start the endpoint with `potluck serve-openai --port {target.port}`",
    ]
    if target.model is None:
        lines.append(
            f"no --model given, so no {ANTHROPIC_MODEL_ENV} is emitted and Claude "
            "Code will send its own default model name, which this pool may not "
            f"serve; list the pool's models with `curl -s {target.models_url}` and "
            "re-run with --model <name>"
        )
    return tuple(lines)
