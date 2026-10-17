"""The tool-facing wire shapes: what a tool's body means and what it gets back.

These are the parsing and rendering decisions :mod:`aipotluck.localapi` cannot
make on its own — which parameters the signed path can carry, how Anthropic's
``system`` field becomes a conversation turn, and when a token count may be
rendered at all. The documents themselves are asserted end to end in
``tests/test_localapi.py``, over a real socket; what is tested here is the policy
behind them, which is where a silent wrong answer would hide.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from aipotluck.errors import ProtocolError
from aipotluck.openai_wire import (
    ANTHROPIC_DEFAULT_MAX_TOKENS,
    ChatCompletionRequest,
    Completion,
    MessagesRequest,
    UnforwardableParameterError,
    aggregate_models,
    anthropic_sse,
    anthropic_usage,
    measurement_split,
    openai_usage,
    parse_body,
    render_anthropic_error,
    render_chunk,
    render_error,
    settled_tokens,
    sse_data,
)
from aipotluck.wire import ChatMessage

MODEL = "coding-pool"


def _body(**overrides: Any) -> dict[str, Any]:
    """Return one minimal OpenAI chat-completions body."""
    document: dict[str, Any] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "what settles a request?"}],
    }
    document.update(overrides)
    return document


# --------------------------------------------------------------------------- #
# reading a tool's body
# --------------------------------------------------------------------------- #


def test_parse_body_accepts_the_fractions_the_strict_parser_refuses() -> None:
    """A tool's body is not a payload anybody signs, so it is read as JSON.

    `canonical.loads_strict` refuses every fractional number by design. Refusing
    ``temperature: 0.7`` here would refuse a body OpenAI's own API accepts, which
    is not a guarantee this surface owes anybody — the fraction is dealt with at
    the point where it would be signed, and named there.
    """
    parsed = parse_body(b'{"model": "m", "temperature": 0.7}')

    assert parsed == {"model": "m", "temperature": 0.7}


def test_parse_body_refuses_a_repeated_key_and_a_non_object() -> None:
    """Two parsers must not be able to read one body differently."""
    with pytest.raises(ProtocolError, match="duplicate JSON key"):
        parse_body(b'{"model": "a", "model": "b"}')
    with pytest.raises(ProtocolError, match="must be a JSON object"):
        parse_body(b"[1, 2]")
    with pytest.raises(ProtocolError, match="not valid JSON"):
        parse_body(b"{")


def test_a_conversation_reaches_the_platform_turn_by_turn_and_verbatim() -> None:
    """Every role and every structured content part survives the parse.

    This is the property the whole surface exists for: a tool's own multi-turn
    context, including a multimodal content part, is forwarded rather than
    flattened into one prompt string.
    """
    request = ChatCompletionRequest.from_body(
        _body(
            messages=[
                {"role": "system", "content": "answer in one word"},
                {"role": "user", "content": "what settles a request?"},
                {"role": "assistant", "content": "a receipt"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "and what backs it?"},
                        {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
                    ],
                },
                {"role": "tool", "content": None, "tool_call_id": "call-1"},
            ]
        )
    )

    assert request.messages == (
        ChatMessage(role="system", content="answer in one word"),
        ChatMessage(role="user", content="what settles a request?"),
        ChatMessage(role="assistant", content="a receipt"),
        ChatMessage(
            role="user",
            content=(
                {"type": "text", "text": "and what backs it?"},
                {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
            ),
        ),
        ChatMessage(role="tool", content=None, tool_call_id="call-1"),
    )


def test_an_unknown_role_is_refused_at_the_boundary() -> None:
    """A role the platform does not forward is a `400`, not an opaque upstream failure."""
    with pytest.raises(ProtocolError, match="unknown message role"):
        ChatCompletionRequest.from_body(
            _body(messages=[{"role": "narrator", "content": "hello"}])
        )


def test_the_delivery_mode_follows_the_stream_flag_and_nothing_else() -> None:
    """Buffered and streaming are two different requests, never conflated."""
    assert ChatCompletionRequest.from_body(_body()).delivery_mode == "buffered_complete"
    assert (
        ChatCompletionRequest.from_body(_body(stream=True)).delivery_mode == "stream_strict"
    )
    with pytest.raises(ProtocolError, match="stream must be a boolean"):
        ChatCompletionRequest.from_body(_body(stream="yes"))


def test_only_the_platforms_own_parameter_set_is_forwarded() -> None:
    """Unlisted fields and explicit nulls are dropped at the boundary.

    ``n`` and ``tools`` are ignored because this surface produces one text
    choice; a null is dropped because a parameter the caller has no opinion
    about must not override the upstream's own default.
    """
    request = ChatCompletionRequest.from_body(
        _body(n=3, tools=[{"type": "function"}], temperature=None, seed=7, stop=["END"])
    )

    assert request.parameters == {"seed": 7, "stop": ["END"]}


def test_a_whole_number_arriving_as_a_float_is_forwarded_as_an_integer() -> None:
    """`1.0` and `1` are the same JSON number, so the signed path can carry it."""
    request = ChatCompletionRequest.from_body(_body(temperature=1.0, top_p=2.0))

    assert request.parameters == {"temperature": 1, "top_p": 2}


def test_every_fractional_parameter_is_refused_at_once_before_anything_is_signed() -> None:
    """One refusal names all of them, and points at the surface that can carry one.

    Reported one per round trip, a caller would fix a body four times for one
    mistake. The refusal is raised while the body is still being parsed, so no
    authorization is minted and nothing is billed; the message names the
    server's own OpenAI-compatible surface, which parses a tool's body with the
    standard library and therefore *can* forward a fraction.
    """
    with pytest.raises(UnforwardableParameterError) as raised:
        ChatCompletionRequest.from_body(
            _body(temperature=0.7, top_p=0.9, presence_penalty=0.5, seed=7)
        )

    error = raised.value
    assert error.parameters == ("presence_penalty", "temperature", "top_p")
    message = str(error)
    for name in error.parameters:
        assert name in message, "every refused parameter is named"
    assert "/openai/v1" in message, "the way out is named, not only the obstacle"
    assert "seed" not in message, "a parameter that can be forwarded is not blamed"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_number_no_upstream_would_accept_is_refused_with_the_fractions(value: float) -> None:
    """NaN and both infinities are not a temperature, and are not forwarded."""
    with pytest.raises(UnforwardableParameterError):
        ChatCompletionRequest.from_body(_body(temperature=value))


def test_a_value_the_canonical_encoder_cannot_carry_is_refused_before_signing() -> None:
    """A float inside a content part is a `400`, not a failure after an authorization."""
    with pytest.raises(ProtocolError, match="cannot forward"):
        ChatCompletionRequest.from_body(
            _body(
                messages=[
                    {"role": "user", "content": [{"type": "text", "weight": 0.5}]},
                ]
            )
        )


# --------------------------------------------------------------------------- #
# Anthropic's shape, folded onto the same conversation model
# --------------------------------------------------------------------------- #


def test_anthropic_system_instructions_become_the_first_turn_of_the_conversation() -> None:
    """A string and a list of blocks are both accepted, because the API documents both."""
    from_string = MessagesRequest.from_body(
        {"model": MODEL, "system": "answer in one word",
         "messages": [{"role": "user", "content": "hi"}]}
    )
    from_blocks = MessagesRequest.from_body(
        {"model": MODEL,
         "system": [{"type": "text", "text": "answer in one word"}],
         "messages": [{"role": "user", "content": "hi"}]}
    )

    assert from_string.messages[0] == ChatMessage(
        role="system", content="answer in one word"
    )
    assert from_blocks.messages[0] == ChatMessage(
        role="system", content=({"type": "text", "text": "answer in one word"},)
    )
    assert [message.role for message in from_string.messages] == ["system", "user"]


def test_an_absent_max_tokens_gets_the_documented_default_rather_than_a_refusal() -> None:
    """Anthropic requires the field; a tool that omits it still gets a ceiling."""
    omitted = MessagesRequest.from_body(
        {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}
    )
    named = MessagesRequest.from_body(
        {"model": MODEL, "max_tokens": 4096,
         "messages": [{"role": "user", "content": "hi"}]}
    )

    assert omitted.parameters["max_tokens"] == ANTHROPIC_DEFAULT_MAX_TOKENS
    assert named.parameters["max_tokens"] == 4096


def test_anthropic_spellings_are_translated_and_its_extras_are_dropped() -> None:
    """``stop_sequences`` is the platform's ``stop``; ``top_k`` is not forwarded."""
    request = MessagesRequest.from_body(
        {
            "model": MODEL,
            "max_tokens": 64,
            "stop_sequences": ["END"],
            "top_k": 40,
            "metadata": {"user_id": "someone"},
            "messages": [{"role": "user", "content": "hi"}],
        }
    )

    assert request.parameters == {"max_tokens": 64, "stop": ["END"]}


def test_anthropic_fractions_are_refused_by_the_same_rule() -> None:
    """Claude Code sends ``temperature: 0.7``; it is refused, not silently dropped."""
    with pytest.raises(UnforwardableParameterError) as raised:
        MessagesRequest.from_body(
            {"model": MODEL, "max_tokens": 64, "temperature": 0.7,
             "messages": [{"role": "user", "content": "hi"}]}
        )

    assert raised.value.parameters == ("temperature",)


# --------------------------------------------------------------------------- #
# what a token count may claim
# --------------------------------------------------------------------------- #


def _status(*, billed: int | None, status: str = "SUCCEEDED") -> dict[str, Any]:
    """Return a request status document whose winning attempt billed `billed`."""
    attempt: dict[str, Any] = {"attempt_id": "A1"}
    if billed is not None:
        attempt["usage_tokens"] = billed
    return {
        "status": status,
        "selected_attempt_id": "A1",
        "attempts": [attempt, {"attempt_id": "A2", "usage_tokens": 999_999}],
    }


def test_usage_is_omitted_when_the_platform_reported_nothing() -> None:
    """Zeros in a billing document read as "this was free", which is a claim.

    Neither a billed total nor a node-reported split exists here, so the block is
    left out rather than filled with a number nobody measured.
    """
    assert openai_usage(_status(billed=None), None) is None
    assert anthropic_usage(None) is None


def test_the_billed_total_is_read_off_the_winning_attempt_not_summed_from_halves() -> None:
    """The number a caller reads is the number the receipt behind it names."""
    usage = openai_usage(_status(billed=1234), {"prompt_tokens": 80, "completion_tokens": 7})

    assert usage == {
        "prompt_tokens": 80,
        "completion_tokens": 7,
        "total_tokens": 1234,
    }
    assert settled_tokens(_status(billed=1234)) == 1234


def test_a_total_without_a_split_leaves_the_halves_at_zero_rather_than_guessing() -> None:
    """Splitting a billed total would invent a proportion nobody reported."""
    assert openai_usage(_status(billed=500), None) == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 500,
    }


def test_a_split_without_a_billed_total_still_renders_the_halves() -> None:
    """The node measured the call even though no settlement row was readable."""
    split = measurement_split({"usage": {"input_total": 11, "output_total": 22}})

    assert split == {"prompt_tokens": 11, "completion_tokens": 22}
    assert openai_usage(_status(billed=None), split) == {
        "prompt_tokens": 11,
        "completion_tokens": 22,
        "total_tokens": 33,
    }
    assert anthropic_usage(split) == {"input_tokens": 11, "output_tokens": 22}


def test_anthropic_usage_needs_a_split_because_its_shape_has_no_total() -> None:
    """A billed total alone cannot be expressed without inventing a proportion."""
    assert anthropic_usage(None) is None
    assert openai_usage({"status": "SUCCEEDED"}, None) is None


# --------------------------------------------------------------------------- #
# models, and the frames a stream is made of
# --------------------------------------------------------------------------- #


def test_only_models_a_connected_sharing_node_advertises_are_listed() -> None:
    """Advertising a model nobody will run turns a typo into a request that waits."""
    models = aggregate_models(
        [
            {"model": "b-pool", "share_enabled": True,
             "last_seen_at": "2026-01-02T00:00:00+00:00"},
            {"model": "a-pool", "share_enabled": True, "last_seen_at": "not-a-timestamp"},
            {"model": "paused-pool", "share_enabled": False},
            {"model": "", "share_enabled": True},
            {"share_enabled": True},
            "not-a-node",
        ]
    )

    assert [model["id"] for model in models] == ["a-pool", "b-pool"]
    assert models[0]["created"] == 0, "an unparseable instant is 0, not an invented one"
    assert models[0]["owned_by"] == "ai-potluck"
    assert models[1]["created"] > 0


def test_a_model_several_nodes_advertise_is_listed_once_at_its_latest_advertisement() -> None:
    """One document per model, stamped with the newest instant the pool advertised it."""
    models = aggregate_models(
        [
            {"model": "pool", "share_enabled": True,
             "last_seen_at": "2026-01-01T00:00:00+00:00"},
            {"model": "pool", "share_enabled": True,
             "last_seen_at": "2026-01-03T00:00:00+00:00"},
        ]
    )

    assert len(models) == 1
    assert models[0]["created"] == int(
        dt.datetime(2026, 1, 3, tzinfo=dt.UTC).timestamp()
    )


def test_a_terminal_usage_frame_carries_no_choices() -> None:
    """That is how OpenAI marks the totals frame apart from an empty increment."""
    chunk = render_chunk(
        Completion(id="chatcmpl-1", created=1700000000, model=MODEL),
        usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    )

    assert chunk["choices"] == []
    assert chunk["usage"]["total_tokens"] == 3
    assert chunk["id"] == "chatcmpl-1"


def test_sse_frames_are_canonical_and_anthropic_names_its_event_twice_consistently() -> None:
    """The `event:` line and the document's own `type` are written from one value."""
    openai_frame = sse_data({"object": "chat.completion.chunk", "choices": []})
    anthropic_frame = anthropic_sse({"type": "content_block_stop", "index": 0})

    assert openai_frame.startswith(b"data: ")
    assert openai_frame.endswith(b"\n\n")
    assert json.loads(openai_frame[len(b"data: ") : -2]) == {
        "object": "chat.completion.chunk",
        "choices": [],
    }
    assert anthropic_frame == (
        b'event: content_block_stop\ndata: {"index":0,"type":"content_block_stop"}\n\n'
    )


def test_both_error_shapes_carry_the_request_id_a_caller_can_verify_with() -> None:
    """A request that outlived the connection is still on the books."""
    openai_error = render_error("did not finish", "upstream_failed", request_id="req-1")
    anthropic_error = render_anthropic_error(
        "did not finish", "model_not_found", request_id="req-1"
    )

    assert openai_error == {
        "error": {
            "message": "did not finish",
            "type": "server_error",
            "code": "upstream_failed",
            "param": None,
            "request_id": "req-1",
        }
    }
    assert anthropic_error == {
        "type": "error",
        "error": {
            "type": "not_found_error",
            "message": "did not finish",
            "request_id": "req-1",
        },
    }
    assert render_error("x", "a_code_nobody_defined")["error"]["type"] == "api_error"
