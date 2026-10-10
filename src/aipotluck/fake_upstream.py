"""Controllable fake upstream for exercising abnormal paths.

Real providers are the wrong place to test a duplicate SSE snapshot or a
missing final usage block. This adapter can be told to drop the terminal usage,
repeat a cumulative snapshot, break mid-stream, or stall before the first token,
and it counts exactly how many upstream calls it received.

That call counter is what makes "no extra model call" assertions real rather
than inferred from the absence of an error.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

from .metering import NormalizedUsage, normalize_anthropic_usage, normalize_openai_usage

FaultMode = Literal[
    "none",
    "drop_final_usage",
    "duplicate_cumulative_snapshot",
    "break_before_first_token",
    "error_before_output",
    "truncate_mid_stream",
    "quota_exhausted",
]


@dataclass
class FakeResponse:
    """One modelled upstream response."""

    text_chunks: tuple[str, ...]
    usage: NormalizedUsage | None
    provider_style: Literal["openai", "anthropic"] = "openai"


@dataclass
class FakeUpstream:
    """A scriptable upstream that records how many calls it received."""

    responses: list[FakeResponse] = field(default_factory=list)
    fault: FaultMode = "none"
    call_count: int = 0
    _cursor: int = 0
    #: Seconds to sleep before emitting each streamed block. Zero means the
    #: stream is produced as fast as the caller consumes it.
    stream_delay_s: float = 0.0
    #: When set, re-split the whole response text into this many blocks instead
    #: of using the response's own `text_chunks`, so a test can dial the block
    #: count independently of the scripted answer.
    stream_blocks: int | None = None

    def queue(self, response: FakeResponse) -> None:
        """Append a response to be returned by the next call."""
        self.responses.append(response)

    def execute(self, request_digest: str) -> FakeResponse:
        """Consume the next scripted response.

        Args:
            request_digest: Digest of the request, recorded for assertions that
                a retry re-sent the same input.

        Returns:
            The next response.

        Raises:
            RuntimeError: If the script is exhausted, which means the code under
                test made more upstream calls than intended.
        """
        self.call_count += 1
        if self.fault == "quota_exhausted":
            raise UpstreamQuotaExceeded("fake upstream quota exhausted")
        if self.fault == "error_before_output":
            raise UpstreamError("fake upstream failed before any output")
        if self.fault == "break_before_first_token":
            return FakeResponse(text_chunks=(), usage=None)
        if self._cursor >= len(self.responses):
            raise RuntimeError(
                f"fake upstream exhausted after {self.call_count} call(s); "
                "an unexpected extra model call was made"
            )
        response = self.responses[self._cursor]
        self._cursor += 1
        return response

    def stream(self, response: FakeResponse) -> Iterator[dict[str, object]]:
        """Yield SSE-style events for a response, honouring the fault mode.

        Blocks come from the response's own `text_chunks` unless `stream_blocks`
        is set, in which case the whole text is re-split into that many roughly
        equal blocks; `stream_delay_s` paces the blocks so a caller can watch them
        arrive one at a time. Usage, when present, is yielded last, matching a
        provider that reports it in its terminal frame.

        Yields:
            Dicts with a `type` of `delta` or `usage`.

        Raises:
            UpstreamError: On a mid-stream truncation fault.
        """
        if self.fault == "break_before_first_token":
            return
        blocks = self._blocks(response)
        for index, chunk in enumerate(blocks):
            if self.fault == "truncate_mid_stream" and index == len(blocks) // 2:
                raise UpstreamError("fake upstream broke mid-stream")
            if self.stream_delay_s > 0:
                time.sleep(self.stream_delay_s)
            yield {"type": "delta", "text": chunk}
        if response.usage is None or self.fault == "drop_final_usage":
            return
        yield {"type": "usage", "usage": response.usage}

    def _blocks(self, response: FakeResponse) -> list[str]:
        """Return the ordered text blocks to stream for a response."""
        if self.stream_blocks is None:
            return list(response.text_chunks)
        count = max(1, self.stream_blocks)
        text = "".join(response.text_chunks)
        if count == 1 or not text:
            return [text]
        size = -(-len(text) // count)
        return [text[index : index + size] for index in range(0, len(text), size)]


class UpstreamError(RuntimeError):
    """Raised for a modelled upstream failure."""


class UpstreamQuotaExceeded(UpstreamError):
    """Raised when the fake upstream's quota is exhausted."""


def openai_usage_payload(
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int | None = None,
    reasoning_tokens: int | None = None,
) -> dict[str, object]:
    """Build an OpenAI-shaped usage payload."""
    prompt_details: dict[str, object] = {}
    if cached_tokens is not None:
        prompt_details["cached_tokens"] = cached_tokens
    completion_details: dict[str, object] = {}
    if reasoning_tokens is not None:
        completion_details["reasoning_tokens"] = reasoning_tokens
    payload: dict[str, object] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
    if prompt_details:
        payload["prompt_tokens_details"] = prompt_details
    if completion_details:
        payload["completion_tokens_details"] = completion_details
    return payload


def anthropic_usage_payload(
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_creation: int = 0,
) -> dict[str, object]:
    """Build an Anthropic-shaped usage payload."""
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
    }


def normalize_by_style(style: str, payload: dict[str, object]) -> NormalizedUsage:
    """Normalize a raw usage payload according to the provider style.

    Args:
        style: `openai` or `anthropic`.
        payload: The upstream usage mapping.

    Returns:
        The normalized usage.

    Raises:
        ValueError: If the style is unknown.
    """
    if style == "openai":
        return normalize_openai_usage(payload)
    if style == "anthropic":
        return normalize_anthropic_usage(payload)
    raise ValueError(f"unknown provider style: {style}")