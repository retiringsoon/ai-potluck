"""Import the upstream a contributor already configured into this node's config.

Why this exists
---------------
A contributor who already runs Claude Code or Codex has an upstream endpoint, a
model and an API key sitting in that tool's own configuration. Retyping them into
``potluck share setup`` makes a second copy of a credential, and the second copy
is the one that goes stale: the tool keeps working against a rotated key while
this node keeps offering the pool an upstream it can no longer reach. Reading the
file the tool itself reads leaves one credential in one place, and configures a
node in one command instead of a transcription exercise.

Three sources are read, each the way its own tool writes it:

* Claude Code — ``~/.claude/settings.json``, whose ``env`` block names
  ``ANTHROPIC_BASE_URL``, ``ANTHROPIC_AUTH_TOKEN`` (or ``ANTHROPIC_API_KEY``) and
  ``ANTHROPIC_MODEL``. The upstream is Anthropic-shaped.
* Codex — ``~/.codex/config.toml``, whose ``model``, ``model_provider`` and
  ``model_providers.<name>.base_url``/``env_key`` name the same three things. The
  credential is read from the environment variable ``env_key`` names, which is
  where Codex reads it too, so this import copies a reference to a key rather
  than becoming a second place one is stored.
* The environment — the ``OPENAI_*`` triple, or the ``ANTHROPIC_*`` one; the
  family that names a base URL decides the provider style.

A tool names a *base* URL and this node's adapter POSTs to exactly the URL it is
given, so completing the path is part of the import: an imported configuration
that merely looked right would fail every attempt the server dispatched. The
provider style is a property of the source and is not offered as an override,
because a node that posts a chat-completions body at an Anthropic upstream fails
loudly at the upstream and silently at the pool.

Fail-loud rules
---------------
A source that does not yield a field is reported by the name its owner can look
up — the key path in the file, or the environment variable — together with the
argument that would supply it, and *every* missing field of one source is
reported at once: fixing three fields in three runs of one command teaches a
contributor nothing about their own file. A field that is present but of the
wrong type is a malformed source and is refused as one rather than read as
absent, because ``model = 3`` is a typo somebody needs to see. Nothing is guessed
and no credential has a default. An incomplete import writes no configuration.

Secrecy: this module reads a credential and returns it inside an `Upstream`.
Nothing here prints, logs, or puts one in an error message;
:meth:`Upstream.summary` is the supported rendering and it names the endpoint,
the model, the wire style and the source.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Literal

from .deploy import absolute_url
from .errors import PotluckError

#: Where Claude Code keeps the settings this command reads. Unexpanded, so a
#: message names the path its reader recognises.
CLAUDE_CODE_SETTINGS: Final = Path("~/.claude/settings.json")

#: Where Codex keeps its configuration.
CODEX_CONFIG: Final = Path("~/.codex/config.toml")

#: The ``env`` keys of Claude Code's settings that name an upstream. Both
#: spellings of the credential are read because Claude Code sends one as a bearer
#: header and the other as ``x-api-key``, and contributors use both.
CLAUDE_CODE_BASE_URL: Final = "ANTHROPIC_BASE_URL"
CLAUDE_CODE_API_KEYS: Final = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
CLAUDE_CODE_MODEL: Final = "ANTHROPIC_MODEL"

#: The path each provider style's completion endpoint ends with.
ANTHROPIC_MESSAGES_PATH: Final = "/v1/messages"
OPENAI_COMPLETIONS_PATH: Final = "/chat/completions"

#: The three fields an upstream is, in the order a message lists them.
UPSTREAM_FIELDS: Final = ("endpoint", "api_key", "model")

#: How each field can be supplied when a source does not name it. Named here
#: rather than at each reader so one flag cannot be spelled two ways.
FIELD_REMEDIES: Final[dict[str, str]] = {
    "endpoint": "--upstream-endpoint",
    "api_key": "--upstream-key-file or AIPOTLUCK_UPSTREAM_KEY",
    "model": "--upstream-model",
}

ProviderStyle = Literal["openai", "anthropic"]


@dataclass(frozen=True)
class EnvFamily:
    """One family of environment variables that names an upstream.

    Attributes:
        label: How the family is named in a message.
        provider_style: The wire style its endpoints speak.
        base_url: The variables that may hold a base URL.
        api_keys: The variables that may hold the credential, in priority order.
        models: The variables that may hold the model.
    """

    label: str
    provider_style: ProviderStyle
    base_url: tuple[str, ...]
    api_keys: tuple[str, ...]
    models: tuple[str, ...]


#: The families ``--from-env`` reads, in the order they are tried. The first one
#: that names a base URL wins, so a shell exporting both is read as the family its
#: endpoint belongs to rather than as an arbitrary pick.
ENVIRONMENT_FAMILIES: Final = (
    EnvFamily(
        label="OPENAI_*",
        provider_style="openai",
        base_url=("OPENAI_BASE_URL",),
        api_keys=("OPENAI_API_KEY",),
        models=("OPENAI_MODEL",),
    ),
    EnvFamily(
        label="ANTHROPIC_*",
        provider_style="anthropic",
        base_url=("ANTHROPIC_BASE_URL",),
        api_keys=CLAUDE_CODE_API_KEYS,
        models=("ANTHROPIC_MODEL",),
    ),
)


class ConfigImportError(PotluckError):
    """Raised when a configuration source cannot yield a usable upstream.

    A distinct type rather than a message match: an incomplete import is a
    statement about somebody else's file, and a caller must be able to tell it
    apart from a node configuration this command could not write.
    """


@dataclass(frozen=True)
class MissingField:
    """One field a source did not yield, named the way its owner can find it.

    Attributes:
        field: The field of `Upstream` that is still empty.
        looked_for: Where the import looked, in the source's own spelling.
        remedy: The argument that would supply the field instead.
    """

    field: str
    looked_for: str
    remedy: str


@dataclass(frozen=True)
class PartialUpstream:
    """What one configuration source yielded, and what it did not.

    A partial result rather than an immediate refusal so an explicit argument can
    fill a gap: a contributor whose tool configuration names no model imports the
    endpoint and the credential and supplies the model, instead of editing a file
    that belongs to another program.

    Attributes:
        source: Where this came from, as a phrase for a message.
        provider_style: The wire style the source's own family implies.
        endpoint: The full completion URL, or `None`.
        api_key: The upstream credential, or `None`. Never rendered.
        model: The model this node would execute, or `None`.
        looked_for: Where each field was sought, keyed by field name, so a gap can
            be reported in the source's own spelling.
    """

    source: str
    provider_style: ProviderStyle
    endpoint: str | None = None
    api_key: str | None = None
    model: str | None = None
    looked_for: Mapping[str, str] = field(default_factory=dict)

    def gaps(self) -> tuple[MissingField, ...]:
        """Return every field this source did not yield, named for its owner."""
        gaps: list[MissingField] = []
        for name in UPSTREAM_FIELDS:
            if getattr(self, name) is not None:
                continue
            gaps.append(
                MissingField(
                    field=name,
                    looked_for=self.looked_for.get(name, f"the {name} of {self.source}"),
                    remedy=FIELD_REMEDIES[name],
                )
            )
        return tuple(gaps)


@dataclass(frozen=True)
class Upstream:
    """One complete upstream, as this node will execute against it.

    Attributes:
        endpoint: The full URL the adapter POSTs to.
        api_key: The upstream credential. Never rendered by this module.
        model: The model this node executes.
        provider_style: The wire style of the upstream.
        name: The label this upstream carries in diagnostics.
        source: Where the configuration came from, for the message that reports it.
    """

    endpoint: str
    api_key: str
    model: str
    provider_style: ProviderStyle
    name: str
    source: str

    def summary(self) -> str:
        """Return the one-line description of this upstream, without the credential.

        The supported rendering of an import: an endpoint, a model, a wire style
        and a source are configuration, and the key is none of those.
        """
        return (
            f"upstream {self.endpoint} / model {self.model} / style "
            f"{self.provider_style} (from {self.source})"
        )

    def as_mapping(self) -> dict[str, str]:
        """Return the fields the node configuration's ``adapter`` block carries."""
        return {
            "endpoint": self.endpoint,
            "api_key": self.api_key,
            "model": self.model,
            "provider_style": self.provider_style,
            "name": self.name,
        }


def completion_endpoint(base_url: str, provider_style: ProviderStyle) -> str:
    """Return the full completion URL for one tool's base URL.

    Args:
        base_url: The base URL the tool was configured with.
        provider_style: Which provider's path completes it.

    Returns:
        The URL this node's adapter POSTs to.

    Raises:
        ConfigImportError: If the URL is not an absolute http(s) URL, or names
            OpenAI's legacy completions endpoint — the adapter posts a
            chat-completions body, so a base URL that already ends in a different
            endpoint's path is one this import must not guess at.
    """
    _require_absolute(base_url, what="the imported upstream base URL")
    trimmed = base_url.rstrip("/")
    if provider_style == "anthropic":
        if trimmed.endswith(ANTHROPIC_MESSAGES_PATH):
            return trimmed
        # A base URL often already carries the version segment, in which case only
        # the route is missing; appending the whole path would produce `/v1/v1/`.
        if trimmed.endswith("/v1"):
            return f"{trimmed}/messages"
        return f"{trimmed}{ANTHROPIC_MESSAGES_PATH}"
    if trimmed.endswith(OPENAI_COMPLETIONS_PATH):
        return trimmed
    if trimmed.endswith("/completions"):
        raise ConfigImportError(
            f"the imported base URL {base_url!r} names OpenAI's legacy completions "
            "endpoint, and this node's adapter posts a chat-completions body; give "
            "the base URL (for example https://api.example/v1) or the full "
            f"{OPENAI_COMPLETIONS_PATH} URL"
        )
    return f"{trimmed}{OPENAI_COMPLETIONS_PATH}"


def read_claude_code(path: Path) -> PartialUpstream:
    """Read the upstream out of Claude Code's ``settings.json``.

    Args:
        path: The settings file; ``~`` is expanded.

    Returns:
        What the file yielded, with every field it does not name reported by
        :meth:`PartialUpstream.gaps`.

    Raises:
        ConfigImportError: If the file cannot be read, is not a JSON object, or
            carries a field of the wrong type.
    """
    source_path = path.expanduser()
    document = _read_json_object(source_path, what="Claude Code settings")
    env = _object_field(document, "env", looked_for=f"'env' in {source_path}")
    looked_for = {
        "endpoint": f"env.{CLAUDE_CODE_BASE_URL} in {source_path}",
        "api_key": _names(
            tuple(f"env.{key}" for key in CLAUDE_CODE_API_KEYS), f"in {source_path}"
        ),
        "model": f"env.{CLAUDE_CODE_MODEL} in {source_path}",
    }
    base_url = _string_field(
        env, CLAUDE_CODE_BASE_URL, looked_for=looked_for["endpoint"]
    )
    return PartialUpstream(
        source=f"Claude Code settings {source_path}",
        provider_style="anthropic",
        endpoint=completion_endpoint(base_url, "anthropic") if base_url else None,
        api_key=_first_field(env, CLAUDE_CODE_API_KEYS, looked_for=looked_for["api_key"]),
        model=_string_field(env, CLAUDE_CODE_MODEL, looked_for=looked_for["model"]),
        looked_for=looked_for,
    )


def read_codex_config(path: Path, environ: Mapping[str, str]) -> PartialUpstream:
    """Read the upstream out of Codex's ``config.toml``.

    Args:
        path: The configuration file; ``~`` is expanded.
        environ: The environment the credential variable is read from.

    Returns:
        What the file yielded, with every field it does not name reported by
        :meth:`PartialUpstream.gaps`.

    Raises:
        ConfigImportError: If the file cannot be read, is not valid TOML, names a
            provider it does not define, declares a wire API this platform's
            adapter does not speak, or carries a field of the wrong type.
    """
    source_path = path.expanduser()
    document = _read_toml_object(source_path)
    in_file = f"in {source_path}"
    model = _string_field(document, "model", looked_for=f"'model' {in_file}")
    provider = _string_field(
        document, "model_provider", looked_for=f"'model_provider' {in_file}"
    )
    providers = _object_field(
        document, "model_providers", looked_for=f"'model_providers' {in_file}"
    )
    base_url: str | None = None
    api_key: str | None = None
    if provider is None:
        # No provider named, so no entry to read a base URL or an env_key from.
        # Both gaps are reported against the field that would have named them.
        looked_for_url = looked_for_key = (
            f"'model_provider' {in_file}, which names the 'model_providers' entry "
            "holding base_url and env_key"
        )
    else:
        if provider not in providers:
            raise ConfigImportError(
                f"model_provider is {provider!r} {in_file}, but 'model_providers' "
                f"defines {sorted(providers) or 'nothing'}; a provider Codex itself "
                "could not resolve is not one this import guesses at"
            )
        entry = f"model_providers.{provider}"
        block = _object_field(providers, provider, looked_for=f"{entry} {in_file}")
        wire_api = _string_field(
            block, "wire_api", looked_for=f"{entry}.wire_api {in_file}"
        )
        if wire_api is not None and wire_api != "chat":
            raise ConfigImportError(
                f"{entry}.wire_api is {wire_api!r} {in_file}; this node's adapter "
                "posts a chat-completions body, so an upstream that speaks only "
                "that other API cannot be shared through it"
            )
        looked_for_url = f"{entry}.base_url {in_file}"
        base_url = _string_field(block, "base_url", looked_for=looked_for_url)
        key_name = _string_field(
            block, "env_key", looked_for=f"{entry}.env_key {in_file}"
        )
        if key_name is None:
            looked_for_key = (
                f"{entry}.env_key {in_file}, which names the environment variable "
                "holding the credential"
            )
        elif environ.get(key_name, "").strip():
            api_key = environ[key_name].strip()
            looked_for_key = ""
        else:
            looked_for_key = (
                f"the environment variable {key_name}, named by {entry}.env_key "
                f"{in_file}"
            )
    return PartialUpstream(
        source=f"Codex config {source_path}",
        provider_style="openai",
        endpoint=completion_endpoint(base_url, "openai") if base_url else None,
        api_key=api_key,
        model=model,
        looked_for={
            "endpoint": looked_for_url,
            "api_key": looked_for_key,
            "model": f"'model' {in_file}",
        },
    )


def read_environment(environ: Mapping[str, str]) -> PartialUpstream:
    """Read the upstream out of the environment.

    Args:
        environ: The environment to read.

    Returns:
        What the first family that names a base URL yielded. When no family does,
        every variable looked for is reported, because "nothing was set" is not an
        actionable answer.
    """
    for family in ENVIRONMENT_FAMILIES:
        base_url = _first_set(environ, family.base_url)
        if base_url is None:
            continue
        api_key = _first_set(environ, family.api_keys)
        model = _first_set(environ, family.models)
        return PartialUpstream(
            source=f"the {family.label} environment variables",
            provider_style=family.provider_style,
            endpoint=completion_endpoint(base_url, family.provider_style),
            api_key=api_key,
            model=model,
            looked_for={
                "endpoint": _names(family.base_url, "in the environment"),
                "api_key": _names(family.api_keys, "in the environment"),
                "model": _names(family.models, "in the environment"),
            },
        )
    return PartialUpstream(
        source="the environment",
        provider_style="openai",
        looked_for={
            "endpoint": "any of "
            + ", ".join(family.base_url[0] for family in ENVIRONMENT_FAMILIES)
            + " in the environment",
            "api_key": "the credential variable of whichever family is set ("
            + ", ".join(name for family in ENVIRONMENT_FAMILIES for name in family.api_keys)
            + ")",
            "model": "the model variable of whichever family is set ("
            + ", ".join(family.models[0] for family in ENVIRONMENT_FAMILIES)
            + ")",
        },
    )


def resolve_upstream(
    partial: PartialUpstream,
    *,
    endpoint: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    name: str = "main",
) -> Upstream:
    """Complete one source's result from explicit arguments, or fail loud.

    An explicit argument outranks the imported value: the file belongs to another
    program and the instruction belongs to the operator.

    Args:
        partial: What the source yielded.
        endpoint: An explicit full completion URL.
        api_key: An explicit credential, read from a file or the environment by
            the caller and never from a command-line argument.
        model: An explicit model.
        name: The diagnostic label for this upstream.

    Returns:
        The complete upstream.

    Raises:
        ConfigImportError: If a field is still empty — every such field is named
            at once, with the place the import looked and the argument that would
            have supplied it — or if an explicit endpoint is not an absolute
            http(s) URL.
    """
    filled = replace(
        partial,
        endpoint=endpoint or partial.endpoint,
        api_key=api_key or partial.api_key,
        model=model or partial.model,
    )
    if filled.endpoint is None or filled.api_key is None or filled.model is None:
        raise ConfigImportError(_incomplete_message(filled))
    if endpoint is not None:
        # An explicit endpoint never went through `completion_endpoint`, so it is
        # checked here rather than written into a node configuration unread.
        _require_absolute(filled.endpoint, what="the upstream endpoint")
    return Upstream(
        endpoint=filled.endpoint,
        api_key=filled.api_key,
        model=filled.model,
        provider_style=filled.provider_style,
        name=name,
        source=filled.source,
    )


def merge_adapter(document: dict[str, Any], upstream: Upstream) -> dict[str, Any]:
    """Return one node configuration with its ``adapter`` block replaced.

    Everything else in the document is preserved. The platform token, the device
    binding, the local limits and the share switch belong to this node and are not
    an upstream's business, and an import that rewrote them would silently reset a
    contributor's own choices. Extra adapter fields the platform defines —
    ``max_tokens`` today — are preserved for the same reason.

    Args:
        document: The node configuration as read.
        upstream: The imported upstream.

    Returns:
        A new document; the argument is not modified.
    """
    existing = document.get("adapter")
    adapter: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    adapter["kind"] = "http"
    adapter.update(upstream.as_mapping())
    merged = dict(document)
    merged["adapter"] = adapter
    return merged


def _incomplete_message(partial: PartialUpstream) -> str:
    """Explain an import that could not produce an upstream, field by field."""
    listing = "\n".join(
        f"  {gap.field}: looked for {gap.looked_for}; supply it with {gap.remedy}"
        for gap in partial.gaps()
    )
    return (
        f"the upstream import from {partial.source} is incomplete, so no node "
        f"configuration was written:\n{listing}"
    )


def _names(names: tuple[str, ...], where: str) -> str:
    """Name a set of keys the way their owner writes them."""
    return f"{' or '.join(names)} {where}".strip()


def _string_field(
    container: Mapping[str, Any], key: str, *, looked_for: str
) -> str | None:
    """Return one string field of a source document, or `None` when it is absent.

    Raises:
        ConfigImportError: If the field exists and is not a usable string. A
            present-but-wrong value is a typo in somebody's file and is reported
            as one, rather than being quietly read as "not configured".
    """
    if key not in container:
        return None
    value = container[key]
    if not isinstance(value, str):
        raise ConfigImportError(
            f"{looked_for} must be a string, got {type(value).__name__}: {value!r}"
        )
    if not value.strip():
        raise ConfigImportError(f"{looked_for} is empty")
    return value.strip()


def _first_field(
    container: Mapping[str, Any], keys: tuple[str, ...], *, looked_for: str
) -> str | None:
    """Return the first of `keys` the container names, or `None` when it names none."""
    for key in keys:
        value = _string_field(container, key, looked_for=looked_for)
        if value is not None:
            return value
    return None


def _object_field(
    container: Mapping[str, Any], key: str, *, looked_for: str
) -> dict[str, Any]:
    """Return one object field of a source document, or an empty one when absent.

    Raises:
        ConfigImportError: If the field exists and is not an object.
    """
    if key not in container:
        return {}
    value = container[key]
    if not isinstance(value, dict):
        raise ConfigImportError(
            f"{looked_for} must be an object, got {type(value).__name__}"
        )
    return value


def _first_set(environ: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    """Return the first non-empty of `names` in the environment."""
    for name in names:
        value = environ.get(name, "").strip()
        if value:
            return value
    return None


def _require_absolute(url: str, *, what: str) -> None:
    """Refuse a URL this node could not POST to.

    Raises:
        ConfigImportError: If the URL is not an absolute http(s) URL. The
            platform's own check is reused, so an import and ``share run`` cannot
            disagree about what an endpoint is.
    """
    try:
        absolute_url(
            url, what=what, schemes=("http", "https"), context="config import"
        )
    except PotluckError as exc:
        raise ConfigImportError(str(exc)) from exc


def _read_json_object(path: Path, *, what: str) -> dict[str, Any]:
    """Read one third-party JSON file as an object.

    Read with the standard library rather than
    :func:`aipotluck.canonical.loads_strict`: this is another program's file, and
    a fractional number somewhere in it that this import never reads must not
    refuse the whole import. What is required of it is that it holds one object.

    Raises:
        ConfigImportError: If the file cannot be read or is not an object.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigImportError(
            f"the {what} file could not be read: {path}: {exc}"
        ) from exc
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ConfigImportError(
            f"the {what} file {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ConfigImportError(
            f"the {what} file {path} must hold a JSON object, got "
            f"{type(parsed).__name__}"
        )
    return parsed


def _read_toml_object(path: Path) -> dict[str, Any]:
    """Read one TOML file as a mapping.

    Raises:
        ConfigImportError: If the file cannot be read or is not valid TOML.
    """
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise ConfigImportError(
            f"the Codex configuration file could not be read: {path}: {exc}"
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigImportError(
            f"the Codex configuration file {path} is not valid TOML: {exc}"
        ) from exc
