"""Tests for long-horizon task capabilities: reasoning models, tool history, session affinity."""

from __future__ import annotations

import pytest

from aipotluck.adapters import HttpUpstreamAdapter, _openai_delta_text
from aipotluck.cli import _build_parser
from aipotluck.errors import ProtocolError
from aipotluck.wire import (
    decode_chat_parameters,
    encode_chat_parameters,
    parse_chat_messages,
)


def test_chat_message_preserves_tool_calls_and_tool_call_id() -> None:
    """Multi-turn tool history is preserved across payload round-trips."""
    raw_messages = [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "name": "alice", "content": "Calculate 2+2 and write to file."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_calc_1",
                    "type": "function",
                    "function": {"name": "calculator", "arguments": '{"expr": "2+2"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_calc_1", "content": "4"},
        {"role": "assistant", "content": "The result is 4."},
    ]

    messages = parse_chat_messages(raw_messages)
    assert len(messages) == 5

    # Check parsed attributes
    assert messages[1].name == "alice"
    assert messages[2].content is None
    assert messages[2].tool_calls is not None
    assert messages[2].tool_calls[0]["id"] == "call_calc_1"
    assert messages[3].tool_call_id == "call_calc_1"
    assert messages[3].content == "4"

    # Check payload serialization
    serialized = [m.to_payload() for m in messages]
    assert serialized[1]["name"] == "alice"
    assert serialized[2]["tool_calls"][0]["function"]["name"] == "calculator"
    assert serialized[3]["tool_call_id"] == "call_calc_1"


def test_reasoning_effort_parameter_encoding_and_decoding() -> None:
    """reasoning_effort parameter is accepted and round-trips through canonical parameters."""
    raw = {"reasoning_effort": "high", "temperature": 0.7, "ignored_key": "drop_me"}
    encoded = encode_chat_parameters(raw)
    assert "reasoning_effort" in encoded
    assert encoded["reasoning_effort"] == "high"
    assert "ignored_key" not in encoded

    decoded = decode_chat_parameters(encoded)
    assert decoded["reasoning_effort"] == "high"
    assert decoded["temperature"] == 0.7

    # Reject non-string
    with pytest.raises(ProtocolError):
        encode_chat_parameters({"reasoning_effort": 123})


def test_adapter_extracts_reasoning_content_in_streaming() -> None:
    """_openai_delta_text extracts reasoning_content when content is None."""
    # Standard content chunk
    chunk_content = {"choices": [{"delta": {"content": "Hello world"}}]}
    assert _openai_delta_text(chunk_content) == "Hello world"

    # Reasoning chunk (content is None)
    chunk_reasoning = {
        "choices": [{"delta": {"content": None, "reasoning_content": "Thinking step 1"}}]
    }
    assert _openai_delta_text(chunk_reasoning) == "Thinking step 1"

    # Delta with only reasoning_content
    chunk_only_reasoning = {"choices": [{"delta": {"reasoning_content": "Thinking step 2"}}]}
    assert _openai_delta_text(chunk_only_reasoning) == "Thinking step 2"

    # Empty chunk
    assert _openai_delta_text({"choices": [{"delta": {}}]}) == ""


def test_adapter_extracts_reasoning_content_in_buffered() -> None:
    """HttpUpstreamAdapter extracts reasoning_content if content is empty."""
    adapter = HttpUpstreamAdapter(
        endpoint="https://api.example.com/v1",
        api_key="sk-test",
        model="deepseek-r1",
    )
    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "Here is the chain of thought.",
                }
            }
        ]
    }
    result = adapter._from_payload(payload)
    assert result.chunks == ("Here is the chain of thought.",)


def test_potluck_ask_cli_session_id_argument() -> None:
    """potluck ask parser accepts --session-id."""
    parser = _build_parser()
    args = parser.parse_args(["ask", "What is potluck?", "--session-id", "sess-abcdef123456"])
    assert args.question == "What is potluck?"
    assert args.session_id == "sess-abcdef123456"
