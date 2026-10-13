"""The tool-facing HTTP shapes: OpenAI's chat completions and Anthropic's messages.

Why this is not :mod:`aipotluck.wire`
------------------------------------
``wire`` is the platform's own contract — the envelopes a consumer, a server and
a contribution node exchange, canonical-encoded and signed. This module is the
contract a *tool* speaks: the JSON an OpenAI-compatible or an Anthropic-compatible
client posts, and the JSON it expects back. The two are kept apart because they
are parsed by different rules. A tool's body is read with the standard library,
since it routinely carries fractional parameters that :func:`aipotluck.canonical.loads_strict`
refuses by design; everything this module hands *to* the platform goes through
``wire``'s own parsers (:func:`aipotluck.wire.parse_chat_messages` and
:func:`aipotluck.wire.encode_chat_parameters`), so a conversation reaches a
contribution node's upstream verbatim and a parameter reaches it only if the
platform has decided to honour it.

Generation is written down once per shape, and every renderer is total: a
document is either rendered completely or the caller is told why it cannot be.
Two families are served because two families of tools exist — OpenAI's
``POST /v1/chat/completions`` and ``GET /v1/models``, and Anthropic's
``POST /v1/messages``, which is what Claude Code speaks.

Honesty rules the renderers enforce
----------------------------------
* A completed answer is reported with OpenAI's ``finish_reason: "stop"`` and
  Anthropic's ``stop_reason: "end_turn"``. Neither is a claim about why the model
  stopped: the platform carries no stop reason back from an upstream, so the one
  thing a renderer can say is "this answer is complete". A request that did not
  complete is an error document, never a short answer.
* ``usage`` is rendered from what the platform actually reported — the token
  total the server billed, and the input/output split from the contributing
  node's final measurement. When neither exists the block is *omitted*. Zeros
  would be a statement that the call was free, which is a claim about money the
  renderer cannot support.

Secrecy: nothing here reads a credential, a key file, or the environment, so no
document it renders can carry one.

Convergence note: the server renders the same OpenAI shapes from its own gateway
in ``ai-potluck-server``. Unifying the two renderers into one shared definition
is a deliberate later step; until then a shape is written down exactly once per
side, and a change to one is a change to review against the other.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from .canonical import canonicalize
from .errors import CanonicalizationError, ProtocolError
from .wire import (
    CHAT_PARAMETER_KEYS,
    DECIMAL_CHAT_PARAMETERS,
    ChatMessage,
    encode_chat_parameters,
    parse_chat_messages,
)

#: Largest request body this surface reads, in bytes. A body larger than this is
#: refused rather than buffered without limit: the endpoint runs on a laptop next
#: to the tool that called it, and an oversized body is a mistake worth naming.
MAX_BODY_BYTES: Final = 4 * 1024 * 1024

#: ``owned_by`` for every model the endpoint lists. The pool has no vendor: a
#: model is whatever the connected contribution nodes advertise.
MODEL_OWNER: Final = "ai-potluck"

#: OpenAI's ``finish_reason`` and Anthropic's ``stop_reason`` for an answer that
#: is complete. See the module docstring: this is not the upstream's own reason,
#: which the platform does not carry back.
FINISH_REASON_COMPLETE: Final = "stop"
STOP_REASON_COMPLETE: Final = "end_turn"

#: Anthropic's API requires ``max_tokens``; OpenAI's does not. A tool that omits
#: it gets this rather than a refusal, because the platform's own upstream
#: adapter budgets the same default and a request that cannot name a ceiling
#: still has one. It is forwarded like any other parameter, so it is part of the
#: digest the authorization signs.
ANTHROPIC_DEFAULT_MAX_TOKENS: Final = 1024

#: Anthropic's own name for the parameter OpenAI calls ``stop``.
ANTHROPIC_STOP_ALIAS: Final = "stop_sequences"

#: OpenAI error ``code`` to the ``type`` OpenAI documents for it. A code with no
#: entry degrades to ``api_error`` rather than being rendered without a type.
OPENAI_ERROR_TYPE: Final[dict[str, str]] = {
    "invalid_request": "invalid_request_error",
    "model_not_found": "invalid_request_error",
    "not_supported": "not_supported",
    "not_found": "invalid_request_error",
    "method_not_allowed": "invalid_request_error",
    "insufficient_quota": "insufficient_quota",
    "rate_limit_exceeded": "rate_limit_error",
    "upstream_failed": "server_error",
    "deadline_passed": "server_error",
    "service_unavailable": "server_error",
    "internal_error": "server_error",
}

#: The same codes in Anthropic's ``error.type`` vocabulary, which names its
#: families differently (``not_found_error`` rather than ``invalid_request_error``
#: for a route or model it does not have).
ANTHROPIC_ERROR_TYPE: Final[dict[str, str]] = {
    "invalid_request": "invalid_request_error",
    "model_not_found": "not_found_error",
    "not_supported": "not_found_error",
    "not_found": "not_found_error",
    "method_not_allowed": "invalid_request_error",
    "insufficient_quota": "rate_limit_error",
    "rate_limit_exceeded": "rate_limit_error",
    "upstream_failed": "api_error",
    "deadline_passed": "api_error",
    "service_unavailable": "api_error",
    "internal_error": "api_error",
}


class UnforwardableParameterError(ProtocolError):
    """A request named parameters the signed consumer path cannot carry.

    Raised before anything is signed or submitted, so a refused request costs
    nothing. The message names *every* offending parameter at once: reporting
    them one per round trip would make a caller fix a body five times for one
    mistake, and a tool that cannot iterate on a refusal is a tool that hangs.

    Why the platform cannot carry them. The signed consumer path submits its
    body as canonical JSON, and :func:`aipotluck.canonical.canonicalize` refuses
    a float outright — as does the server's own
    :func:`aipotluck.canonical.loads_strict` when it parses that body — so
    ``temperature: 0.7`` cannot be submitted as the number it is. The encoding
    that *does* carry a fraction through canonical JSON is
    :func:`aipotluck.wire.encode_chat_parameters`' shortest round-tripping
    decimal string, but the server re-encodes the parameters of a submitted body
    through that same function, and it accepts a number only: the string
    ``"0.7"`` is refused as "must be a number". A fraction therefore has no
    representation both sides of the signed path accept, and this endpoint says
    so instead of silently dropping a parameter the caller asked for. Whole
    numbers are unaffected — ``temperature: 1.0`` and ``temperature: 1`` are the
    same JSON number — and are forwarded as the integer.
    """

    def __init__(self, names: Sequence[str]) -> None:
        """Record the refused parameter names and build the message from them."""
        self.parameters: tuple[str, ...] = tuple(names)
        listed = ", ".join(self.parameters)
        super().__init__(
            f"this endpoint cannot forward a fractional value for {listed}: the "
            "signed consumer path carries its body as canonical JSON, which refuses "
            "floats, and the server re-reads the parameters as numbers only. Send a "
            "whole number, omit the parameter, or point the tool at the server's own "
            "OpenAI-compatible surface at <server-url>/openai/v1, which parses a "
            "tool's body with the standard library and forwards fractions."
        )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Object hook that refuses a repeated key instead of silently keeping one.

    Raises:
        ValueError: If a key appears twice, which `json.loads` reports as-is.
    """
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate JSON key: {key!r}")
        document[key] = value
    return document


def parse_body(raw: bytes) -> dict[str, Any]:
    """Parse one request body as a JSON object.

    Parsed with the standard library rather than :func:`aipotluck.canonical.loads_strict`:
    a tool's body legitimately carries fractional parameters, which the strict
    parser refuses, and refusing a body OpenAI's own API would accept is not a
    guarantee anybody asked for. Refusing duplicate keys keeps the one property
    of the strict parser this surface still needs — two parsers must not be able
    to read one body differently.

    Args:
        raw: The request body bytes.

    Returns:
        The parsed object.

    Raises:
        ProtocolError: If the body is not valid UTF-8 JSON, or is not an object.
    """
    try:
        parsed = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"the body is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ProtocolError(
            f"the body must be a JSON object, got {type(parsed).__name__}"
        )
    return parsed


def _require_model(body: Mapping[str, Any]) -> str:
    """Return the requested model name.

    Raises:
        ProtocolError: If ``model`` is absent, empty, or not a string.
    """
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise ProtocolError("the body must name a non-empty string 'model'")
    return model


def _require_bool(body: Mapping[str, Any], key: str) -> bool:
    """Return one boolean body field, defaulting to `False`.

    Raises:
        ProtocolError: If the field is present and is not a boolean.
    """
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise ProtocolError(f"{key} must be a boolean")
    return value


def _encode_forwarded(source: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce one body's parameters to the set the platform forwards.

    Args:
        source: The parameters as the tool named them, already translated out of
            any alias.

    Returns:
        The forwarded parameters in the canonical-safe encoding
        :func:`aipotluck.wire.encode_chat_parameters` produces.

    Raises:
        UnforwardableParameterError: If one or more fractional parameters were
            named; every one of them is listed.
        ProtocolError: If a forwarded parameter has the wrong shape.
    """
    prepared: dict[str, Any] = {}
    refused: list[str] = []
    for key in sorted(CHAT_PARAMETER_KEYS):
        if key not in source:
            continue
        value = source[key]
        if value is None:
            # A parameter the caller has no opinion about must not override the
            # upstream's own default, so an explicit null is dropped.
            continue
        if key in DECIMAL_CHAT_PARAMETERS and isinstance(value, float):
            # `value != value` is NaN, and neither infinity is a number an
            # upstream would accept; both are refused with the fractions.
            if value != value or value in (float("inf"), float("-inf")):
                refused.append(key)
            elif value.is_integer():
                prepared[key] = int(value)
            else:
                refused.append(key)
            continue
        prepared[key] = value
    if refused:
        raise UnforwardableParameterError(refused)
    return encode_chat_parameters(prepared)


def _check_canonical(messages: tuple[ChatMessage, ...], parameters: Mapping[str, Any]) -> None:
    """Refuse a request the platform's own encoder could not carry.

    Checked here rather than left to the submit path: signing happens next, and a
    body that fails inside the canonical encoder would fail *after* an
    authorization was minted for it, answering a caller with an encoding error
    instead of a `400` that names the body.

    Raises:
        ProtocolError: If a message or a parameter carries a value the canonical
            encoder refuses (a float inside a content part, say).
    """
    try:
        canonicalize([message.to_payload() for message in messages])
        canonicalize(dict(parameters))
    except CanonicalizationError as exc:
        raise ProtocolError(
            f"the request carries a value this endpoint cannot forward: {exc}"
        ) from exc


def _stream_options(body: Mapping[str, Any]) -> bool:
    """Return whether the caller asked for a terminal usage frame.

    Raises:
        ProtocolError: If ``stream_options`` is not an object, or
            ``include_usage`` is not a boolean.
    """
    options = body.get("stream_options")
    if options is None:
        return False
    if not isinstance(options, dict):
        raise ProtocolError("stream_options must be a JSON object")
    include_usage = options.get("include_usage", False)
    if not isinstance(include_usage, bool):
        raise ProtocolError("stream_options.include_usage must be a boolean")
    return include_usage


@dataclass(frozen=True)
class ChatCompletionRequest:
    """One parsed OpenAI chat-completions body.

    Attributes:
        model: The model requested.
        messages: The caller's own conversation, parsed by the platform's wire
            layer and forwarded to a node verbatim.
        parameters: The forwarded subset, in the canonical-safe encoding.
        stream: Whether the caller asked for SSE chunks.
        include_usage: Whether a streaming caller asked for a terminal usage frame.
    """

    model: str
    messages: tuple[ChatMessage, ...]
    parameters: dict[str, Any]
    stream: bool
    include_usage: bool

    @property
    def delivery_mode(self) -> str:
        """Return the platform delivery mode this body asks for."""
        return "stream_strict" if self.stream else "buffered_complete"

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> ChatCompletionRequest:
        """Parse an OpenAI chat-completions body.

        Args:
            body: The parsed request body.

        Returns:
            The request this endpoint will sign and submit.

        Raises:
            ProtocolError: If the model is missing, the messages are not a
                conversation the platform forwards, a parameter has the wrong
                shape, or a value cannot cross the canonical encoder.
            UnforwardableParameterError: If a fractional parameter was named.
        """
        messages = parse_chat_messages(body.get("messages"))
        parameters = _encode_forwarded(body)
        _check_canonical(messages, parameters)
        return cls(
            model=_require_model(body),
            messages=messages,
            parameters=parameters,
            stream=_require_bool(body, "stream"),
            include_usage=_stream_options(body),
        )


def _anthropic_system_messages(body: Mapping[str, Any]) -> tuple[ChatMessage, ...]:
    """Return the system turn Anthropic's top-level ``system`` field becomes.

    Anthropic takes system instructions as a top-level field rather than a turn;
    the platform's conversation model takes them as a ``system`` message, which is
    the same reshaping the node's upstream adapter performs in the other
    direction. A string and a list of ``{"type": "text"}`` blocks are both
    accepted, because the API documents both.

    Raises:
        ProtocolError: If ``system`` is neither a string nor a list of objects.
    """
    system = body.get("system")
    if system is None:
        return ()
    if isinstance(system, str):
        return (ChatMessage(role="system", content=system),)
    if isinstance(system, list):
        for block in system:
            if not isinstance(block, dict):
                raise ProtocolError(
                    "a system block must be a JSON object, got "
                    f"{type(block).__name__}"
                )
        return (ChatMessage(role="system", content=tuple(system)),)
    raise ProtocolError("system must be a string or a list of content blocks")


@dataclass(frozen=True)
class MessagesRequest:
    """One parsed Anthropic messages body.

    Attributes:
        model: The model requested.
        messages: The conversation, with ``system`` folded in as its first turn.
        parameters: The forwarded subset, always carrying a ``max_tokens``.
        stream: Whether the caller asked for Anthropic's streaming events.
    """

    model: str
    messages: tuple[ChatMessage, ...]
    parameters: dict[str, Any]
    stream: bool

    @property
    def delivery_mode(self) -> str:
        """Return the platform delivery mode this body asks for."""
        return "stream_strict" if self.stream else "buffered_complete"

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> MessagesRequest:
        """Parse an Anthropic messages body.

        Args:
            body: The parsed request body.

        Returns:
            The request this endpoint will sign and submit.

        Raises:
            ProtocolError: If the model is missing, the messages are not a
                conversation the platform forwards, ``system`` has the wrong
                shape, or a parameter cannot cross the canonical encoder.
            UnforwardableParameterError: If a fractional parameter was named.
        """
        messages = _anthropic_system_messages(body) + parse_chat_messages(body.get("messages"))
        # Anthropic's own spelling of the forwarded parameters, translated to the
        # names the platform forwards. A field outside that set — `top_k`,
        # `metadata` — is dropped at the boundary, which is the platform's
        # documented rule rather than this endpoint's invention.
        source: dict[str, Any] = {"max_tokens": ANTHROPIC_DEFAULT_MAX_TOKENS}
        for key in body:
            if key == ANTHROPIC_STOP_ALIAS:
                source["stop"] = body[key]
            else:
                source[key] = body[key]
        parameters = _encode_forwarded(source)
        _check_canonical(messages, parameters)
        return cls(
            model=_require_model(body),
            messages=messages,
            parameters=parameters,
            stream=_require_bool(body, "stream"),
        )


@dataclass(frozen=True)
class Completion:
    """The identity of one rendered completion, fixed before its first frame.

    Every frame of one answer carries the same id, model and creation instant,
    which is what lets a client match a stream to the request that started it.
    """

    id: str
    created: int
    model: str

    @classmethod
    def new(cls, model: str) -> Completion:
        """Mint the identity for one OpenAI-shaped completion."""
        return cls(
            id=f"chatcmpl-{secrets.token_hex(12)}",
            created=int(time.time()),
            model=model,
        )


def render_completion(
    completion: Completion, *, content: str, usage: Mapping[str, int] | None
) -> dict[str, Any]:
    """Render one ``chat.completion`` document.

    Args:
        completion: The identity every frame of this answer carries.
        content: The complete answer text.
        usage: The token counts the platform reported, or `None` to omit them.

    Returns:
        The document, with exactly one choice.
    """
    document: dict[str, Any] = {
        "id": completion.id,
        "object": "chat.completion",
        "created": completion.created,
        "model": completion.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, "refusal": None},
                "finish_reason": FINISH_REASON_COMPLETE,
                "logprobs": None,
            }
        ],
    }
    if usage is not None:
        document["usage"] = dict(usage)
    return document


def render_chunk(
    completion: Completion,
    *,
    delta: Mapping[str, Any] | None = None,
    finish_reason: str | None = None,
    usage: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Render one ``chat.completion.chunk`` document.

    A terminal usage frame carries no choices, which is how OpenAI marks it as
    the totals rather than an empty increment.

    Args:
        completion: The identity every frame of this answer carries.
        delta: The increment, or `None` for a frame that carries none.
        finish_reason: The reason this choice ended, on the final content frame.
        usage: The token counts, on the terminal usage frame.

    Returns:
        The chunk document.
    """
    choices: list[dict[str, Any]] = []
    if delta is not None or finish_reason is not None:
        choices.append(
            {
                "index": 0,
                "delta": dict(delta) if delta is not None else {},
                "finish_reason": finish_reason,
                "logprobs": None,
            }
        )
    document: dict[str, Any] = {
        "id": completion.id,
        "object": "chat.completion.chunk",
        "created": completion.created,
        "model": completion.model,
        "choices": choices,
    }
    if usage is not None:
        document["usage"] = dict(usage)
    return document


def aggregate_models(nodes: Iterable[Any]) -> list[dict[str, Any]]:
    """Aggregate the models the connected, sharing nodes advertise.

    A model no node is sharing is left out rather than listed: this list is what a
    client picks a model from, and advertising one nobody will run turns a typo
    into a request that waits out its deadline. ``created`` is the instant the
    pool last advertised the model — the platform holds no model registration
    date, and a real timestamp it does have beats a zero that only looks like one.

    Args:
        nodes: The entries of the server's node listing.

    Returns:
        One model document per advertised model, sorted by name.
    """
    models: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, dict) or not node.get("share_enabled"):
            continue
        model = node.get("model")
        if not isinstance(model, str) or not model:
            continue
        seen = _epoch_seconds(node.get("last_seen_at"))
        existing = models.get(model)
        if existing is None or seen > int(existing["created"]):
            models[model] = {
                "id": model,
                "object": "model",
                "created": seen,
                "owned_by": MODEL_OWNER,
            }
    return [models[name] for name in sorted(models)]


def _epoch_seconds(value: Any) -> int:
    """Return an ISO-8601 instant as epoch seconds, or 0 when it is unreadable.

    A node listing this endpoint cannot parse a timestamp from still advertises
    its model; inventing a failure here would hide a model the pool can serve.
    """
    if not isinstance(value, str):
        return 0
    try:
        return int(dt.datetime.fromisoformat(value).timestamp())
    except ValueError:
        return 0


def render_model_list(models: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Render OpenAI's list envelope around aggregated model documents."""
    return {"object": "list", "data": [dict(model) for model in models]}


def render_error(
    message: str, code: str, *, request_id: str | None = None
) -> dict[str, Any]:
    """Render one OpenAI error document.

    Args:
        message: The explanation. Callers build it; this module only shapes it.
        code: The machine-readable code, mapped to OpenAI's ``type``.
        request_id: The platform request id, when one exists. It rides in the body
            as well as the ``x-request-id`` header a caller sets, because a
            request that outlived this connection is still on the books.

    Returns:
        The ``{"error": {...}}`` document.
    """
    error: dict[str, Any] = {
        "message": message,
        "type": OPENAI_ERROR_TYPE.get(code, "api_error"),
        "code": code,
        "param": None,
    }
    if request_id is not None:
        error["request_id"] = request_id
    return {"error": error}


def render_anthropic_error(
    message: str, code: str, *, request_id: str | None = None
) -> dict[str, Any]:
    """Render one Anthropic error document.

    Anthropic's error body nests one level deeper than OpenAI's and names its
    families differently; the platform request id is added under the same key so
    a caller can still quote it.
    """
    error: dict[str, Any] = {
        "type": ANTHROPIC_ERROR_TYPE.get(code, "api_error"),
        "message": message,
    }
    if request_id is not None:
        error["request_id"] = request_id
    return {"type": "error", "error": error}


def sse_data(document: Mapping[str, Any]) -> bytes:
    """Render one OpenAI SSE data frame.

    Encoded with :func:`aipotluck.canonical.canonicalize` rather than
    ``json.dumps``: one encoder for everything this package writes, and a float
    that slipped into a document is refused here instead of being serialized
    differently on two surfaces.
    """
    return b"data: " + canonicalize(dict(document)) + b"\n\n"


#: The terminator every OpenAI-compatible stream ends with. It says the stream is
#: over, not that the completion succeeded: an error frame before it is what says
#: the answer is not, and a client whose read loop waits for it would otherwise
#: sit on a closed socket wondering whether more was coming.
SSE_DONE: Final[bytes] = b"data: [DONE]\n\n"


def new_anthropic_message_id() -> str:
    """Mint an Anthropic-shaped message id."""
    return f"msg_{secrets.token_hex(12)}"


def anthropic_message_start(
    *, message_id: str, model: str, usage: Mapping[str, int] | None
) -> dict[str, Any]:
    """Render the ``message_start`` event's document.

    The opening message carries an empty ``content`` list, which the streaming
    events then fill; ``usage`` is whatever the platform has reported so far, and
    is omitted rather than zeroed when it has reported nothing.
    """
    message: dict[str, Any] = {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
    }
    if usage is not None:
        message["usage"] = dict(usage)
    return {"type": "message_start", "message": message}


def anthropic_block_start() -> dict[str, Any]:
    """Render the ``content_block_start`` event's document for one text block."""
    return {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }


def anthropic_text_delta(text: str) -> dict[str, Any]:
    """Render one ``content_block_delta`` event's document."""
    return {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": text},
    }


def anthropic_block_stop() -> dict[str, Any]:
    """Render the ``content_block_stop`` event's document."""
    return {"type": "content_block_stop", "index": 0}


def anthropic_message_delta(*, output_tokens: int | None) -> dict[str, Any]:
    """Render the ``message_delta`` event's document.

    Args:
        output_tokens: The completion tokens the platform reported, or `None` to
            omit the usage block rather than claim zero.
    """
    document: dict[str, Any] = {
        "type": "message_delta",
        "delta": {"stop_reason": STOP_REASON_COMPLETE, "stop_sequence": None},
    }
    if output_tokens is not None:
        document["usage"] = {"output_tokens": output_tokens}
    return document


def anthropic_message_stop() -> dict[str, Any]:
    """Render the terminal ``message_stop`` event's document."""
    return {"type": "message_stop"}


def render_anthropic_message(
    *,
    message_id: str,
    model: str,
    content: str,
    usage: Mapping[str, int] | None,
) -> dict[str, Any]:
    """Render one buffered Anthropic ``message`` document.

    Args:
        message_id: The id this answer was opened with.
        model: The model that answered.
        content: The complete answer text.
        usage: The token counts the platform reported, or `None` to omit them.

    Returns:
        The document, carrying exactly one text block.
    """
    document: dict[str, Any] = {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": content}],
        "stop_reason": STOP_REASON_COMPLETE,
        "stop_sequence": None,
    }
    if usage is not None:
        document["usage"] = dict(usage)
    return document


def anthropic_sse(document: Mapping[str, Any]) -> bytes:
    """Render one Anthropic streaming event.

    Anthropic names the event in an ``event:`` line as well as in the document's
    own ``type``; both are written from the same value, so the two can never
    disagree about what a frame is.
    """
    encoded = canonicalize(dict(document))
    name = str(document["type"])
    return f"event: {name}\n".encode() + b"data: " + encoded + b"\n\n"


def settled_tokens(status: Mapping[str, Any]) -> int | None:
    """Return the token total the server billed for the winning attempt.

    Read off the request's status document, from the attempt the server selected
    — the same row the receipt behind it names, so the number a caller reads is
    the number that was billed rather than a sum of reported halves.
    """
    selected = status.get("selected_attempt_id")
    attempts = status.get("attempts")
    if not isinstance(attempts, list):
        return None
    for attempt in attempts:
        if not isinstance(attempt, dict) or attempt.get("attempt_id") != selected:
            continue
        tokens = attempt.get("usage_tokens")
        return tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else None
    return None


def measurement_split(payload: Mapping[str, Any]) -> dict[str, int] | None:
    """Return the input/output token split of a stored ``usage.final`` payload.

    Returns:
        The split, or `None` when the event carries no readable one, so a caller
        can omit the halves rather than invent them from the billed total.
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    input_total = usage.get("input_total")
    output_total = usage.get("output_total")
    if not isinstance(input_total, int) or not isinstance(output_total, int):
        return None
    return {"prompt_tokens": input_total, "completion_tokens": output_total}


def openai_usage(
    status: Mapping[str, Any], split: Mapping[str, int] | None
) -> dict[str, int] | None:
    """Render the platform's token counts as an OpenAI ``usage`` block.

    Returns:
        The block, or `None` when the platform reported neither a billed total nor
        a split. Omitting it is the honest answer; ``0`` would claim the call was
        free. When only the total is known the halves stay ``0`` rather than
        being guessed from it.
    """
    billed = settled_tokens(status)
    if split is None and billed is None:
        return None
    prompt_tokens = int(split["prompt_tokens"]) if split else 0
    completion_tokens = int(split["completion_tokens"]) if split else 0
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": billed if billed is not None else prompt_tokens + completion_tokens,
    }


def anthropic_usage(split: Mapping[str, int] | None) -> dict[str, int] | None:
    """Render the platform's token counts as an Anthropic ``usage`` block.

    Returns:
        The block, or `None` when the contributing node reported no split.
        Anthropic's shape has no total field, so a billed total alone cannot be
        expressed without inventing a proportion — and an invented proportion in a
        billing document is a fabricated number.
    """
    if split is None:
        return None
    return {
        "input_tokens": int(split["prompt_tokens"]),
        "output_tokens": int(split["completion_tokens"]),
    }
