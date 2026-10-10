#!/usr/bin/env python3
"""A minimal OpenAI-compatible upstream for local smoke tests.

Why this exists: the release gate (``scripts/release_gate.sh``) must exercise the
contributor → center → consumer path with a *real* HTTP upstream, in a real
process, without a provider account. This is that upstream: one endpoint,
``POST /v1/chat/completions``, the two wire modes the node adapter speaks
(``stream: true`` as Server-Sent Events, ``stream: false`` as one JSON body), and
the Bearer check the adapter always sends.

What it never does: it never forwards anything to a real provider, never accepts
a request whose ``Authorization`` header does not match the key it was started
with, and never logs a request header, a request body, or the key. The reply is
deterministic — it echoes the prompt behind a fixed ASCII marker — so a caller
can assert on real end-to-end output instead of a mocked return value.

Deviation from the review appendix A: the port may be ``0``, in which case the
kernel picks a free one and the chosen port is printed as
``dev_fake_upstream: listening on 127.0.0.1:<port>``. The gate reads that line so
parallel runs cannot collide on a fixed port.

``--chunk-delay`` paces the SSE stream. The gate has to prove that a streaming
answer's first increment reaches the consumer *before the request completes*, and
a fixture that writes every frame in one burst makes that unobservable: by the
time the consumer's stdout could be read, the process would already be gone. A
delay of a few hundred milliseconds per frame is what turns that assertion into
something a shell can actually see.

Usage:
    scripts/dev_fake_upstream.py [--host 127.0.0.1] [--port 9101]
                                 [--model coding-pool] [--key sk-smoke-dev-fake]
                                 [--chunk-delay 0.0]

Exit codes: 0 on SIGINT/SIGTERM, 2 on a usage problem (argparse's own status).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: The stable ASCII marker every reply starts with. The release gate greps the
#: consumer's stdout for it, so it must not change without the gate changing too.
REPLY_MARKER = "dev_fake_upstream reply to:"

#: Fixed identifiers in the (otherwise OpenAI-shaped) response documents.
RESPONSE_ID = "chatcmpl-smoke"


def reply_text(prompt: str) -> str:
    """Return the deterministic answer for a prompt."""
    return f"{REPLY_MARKER} {prompt}" if prompt else REPLY_MARKER


def usage_document(prompt: str, reply: str) -> dict[str, int]:
    """Return an OpenAI-shaped usage block with non-zero token totals.

    The counts are character counts, not real tokens. They only have to be
    positive and small: the center settles 1 milli-credit per billed token, so
    a smoke request must stay under the node's local exposure ceiling.
    """
    prompt_tokens = max(1, len(prompt))
    completion_tokens = max(1, len(reply))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _error(message: str) -> dict[str, object]:
    """Return an OpenAI-shaped error document."""
    return {"error": {"message": message, "type": "invalid_request_error"}}


def _prompt_of(body: dict[str, object]) -> str:
    """Return the last user message's text, or the empty string."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else ""
    return ""


class _Handler(BaseHTTPRequestHandler):
    """Serve the one endpoint; every other path is a 404."""

    #: The adapter reads the body incrementally, so the stream must be sent as it
    #: is produced. HTTP/1.0 is deliberate: the response ends at connection close,
    #: which is what lets an SSE body be written without a length it cannot know.
    protocol_version = "HTTP/1.0"

    server: _Server

    def log_message(self, fmt: str, *args: object) -> None:
        """Log only what is safe: method, path and status, never headers or body."""
        sys.stderr.write(f"dev_fake_upstream: {self.command} {self.path} -> {args[1]}\n")

    def do_GET(self) -> None:
        """Answer liveness probes without a credential; everything else is 404."""
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, _error("no such path"))

    def do_POST(self) -> None:
        """Handle ``POST /v1/chat/completions`` in stream or buffered mode."""
        if self.path != "/v1/chat/completions":
            self._discard_body()
            self._send_json(404, _error("no such path"))
            return
        if not self._authorized():
            self._discard_body()
            self._send_json(401, _error("invalid api key"))
            return
        body = self._read_json_body()
        if body is None:
            self._send_json(400, _error("malformed request body"))
            return
        prompt = _prompt_of(body)
        reply = reply_text(prompt)
        if body.get("stream") is True:
            self._send_stream(reply, prompt)
        else:
            self._send_completion(reply, prompt)

    # -- request handling ---------------------------------------------------

    def _authorized(self) -> bool:
        """Whether the presented Bearer token is the one this server was started with."""
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {self.server.api_key}"

    def _read_json_body(self) -> dict[str, object] | None:
        """Read and parse the request body, or ``None`` when it is unusable."""
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return None
        try:
            length = int(raw_length)
        except ValueError:
            return None
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _discard_body(self) -> None:
        """Drain an unread body so a kept-alive client can reuse the connection."""
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return
        with contextlib.suppress(ValueError, OSError):
            self.rfile.read(int(raw_length))

    # -- responses ----------------------------------------------------------

    def _send_json(self, status: int, document: dict[str, object]) -> None:
        """Write one JSON response."""
        payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_completion(self, reply: str, prompt: str) -> None:
        """Write one buffered ``chat.completion`` document."""
        self._send_json(
            200,
            {
                "id": RESPONSE_ID,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": self.server.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": reply},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage_document(prompt, reply),
            },
        )

    def _send_stream(self, reply: str, prompt: str) -> None:
        """Write an SSE stream: word-by-word deltas, one usage frame, then [DONE]."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        words = reply.split(" ")
        for index, word in enumerate(words):
            # Every increment but the last carries its trailing space, so the
            # concatenation of all deltas is exactly `reply`.
            delta = word if index == len(words) - 1 else word + " "
            if self.server.chunk_delay > 0:
                time.sleep(self.server.chunk_delay)
            self._write_frame(
                {
                    "id": RESPONSE_ID,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.server.model,
                    "choices": [
                        {"index": 0, "delta": {"content": delta}, "finish_reason": None}
                    ],
                }
            )
        # The terminal usage frame has no choices; that is what tells the adapter
        # it is the totals, not an empty increment.
        self._write_frame(
            {
                "id": RESPONSE_ID,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": self.server.model,
                "choices": [],
                "usage": usage_document(prompt, reply),
            }
        )
        self._write_raw("data: [DONE]\n\n")

    def _write_frame(self, document: dict[str, object]) -> None:
        """Write one SSE data frame carrying a JSON document."""
        self._write_raw("data: " + json.dumps(document, separators=(",", ":")) + "\n\n")

    def _write_raw(self, text: str) -> None:
        """Write raw SSE bytes and flush so the client sees them immediately."""
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()


class _Server(ThreadingHTTPServer):
    """A threading HTTP server that knows the key and model it was started with."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, address: tuple[str, int], *, api_key: str, model: str, chunk_delay: float = 0.0
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.chunk_delay = chunk_delay
        super().__init__(address, _Handler)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        prog="dev_fake_upstream.py",
        description="A minimal OpenAI-compatible upstream for local smoke tests.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
    parser.add_argument(
        "--port",
        type=int,
        default=9101,
        help="Bind port; 0 asks the kernel for a free one (default 9101).",
    )
    parser.add_argument(
        "--model", default="coding-pool", help="Model name advertised in responses."
    )
    parser.add_argument(
        "--key", default="sk-smoke-dev-fake", help="The only Bearer key accepted."
    )
    parser.add_argument(
        "--chunk-delay",
        type=float,
        default=0.0,
        help=(
            "Seconds to hold back each streamed frame (default 0). A caller that "
            "has to observe the first increment before the stream ends needs "
            "this; the reply content is unchanged."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the fake upstream until SIGINT/SIGTERM."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if not 0 <= args.port <= 65535:
        print(f"dev_fake_upstream: port out of range: {args.port}", file=sys.stderr)
        return 2
    if args.chunk_delay < 0:
        print(f"dev_fake_upstream: negative --chunk-delay: {args.chunk_delay}", file=sys.stderr)
        return 2
    server = _Server(
        (args.host, args.port),
        api_key=args.key,
        model=args.model,
        chunk_delay=args.chunk_delay,
    )
    bound_port = server.server_address[1]
    print(f"dev_fake_upstream: listening on {args.host}:{bound_port} "
          f"(model {args.model})", flush=True)
    stop = threading.Event()

    def _stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    serve = threading.Thread(target=server.serve_forever, daemon=True)
    serve.start()
    try:
        stop.wait()
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
