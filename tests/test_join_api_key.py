"""The client half of an API key an invite issued at join.

The server decides whether an invite issues a key (``potluck-server admin invite
--role consumer --with-api-key``); these tests pin what ``potluck join`` does
with one when the response carries it, and what it does when it does not. Both
are assertions about the screen a person reads and the files left on disk, so the
server on the other end is a loopback stub answering ``POST /v1/join`` with the
document the real one answers with: this package must not import the server
package, and the real server is driven from the server repository's suite.

The property that matters most is what the client does *not* do. It never writes
the plaintext to a file. Nothing this package runs needs an API key — ``ask`` and
``serve-openai`` sign with the identity it already saved — so a stored copy would
be a second place for one credential to be stolen from, for no command's benefit.
The server shows the key exactly once and keeps a digest, and the client's job is
to put that one showing in front of the user with the endpoint it belongs to.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aipotluck import ux
from aipotluck.cli import main

#: A key shaped like the one the server mints: the prefix, then 43 url-safe
#: characters. Recognizable on a line of its own, which is how these tests find it.
API_KEY = "ak-" + "Q" * 43

#: The identity a join response carries. The server derives every field; a client
#: that named its own would be ignored.
SUBJECT = "consumer-9f2c1a"
INVITE = "code-join"


class _StubJoin:
    """A loopback server answering ``POST /v1/join`` with a document a test owns.

    It answers with the identity fields every join response carries, plus
    whatever a test adds — an ``api_key``, or nothing, which is the ordinary
    case. The request bodies it received are kept, so a test can assert on what
    the client put on the wire as well as on what it printed.
    """

    def __init__(self) -> None:
        """Bind a loopback port and start serving."""
        self.bodies: list[bytes] = []
        self.document: dict[str, Any] = {}
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                """Read the body, then answer with the document under test."""
                length = int(self.headers.get("content-length") or 0)
                stub.bodies.append(self.rfile.read(length))
                payload = json.dumps(stub.document).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                """Silence the default stderr access log."""

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{int(self._httpd.server_address[1])}"
        self.document.update({
            "tenant_id": "t-9f2c1a",
            "subject_id": SUBJECT,
            "role": "consumer",
            "key_id": SUBJECT,
            "server_url": self.url,
        })

    def issue_key(self, key: str) -> None:
        """Answer the next join with an API key beside the identity."""
        self.document["api_key"] = key

    def stop(self) -> None:
        """Stop serving and close the port."""
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=10)


@pytest.fixture
def stub_join() -> Iterator[_StubJoin]:
    """A running stub join endpoint, stopped at the end of the test."""
    stub = _StubJoin()
    yield stub
    stub.stop()


def _join(stub: _StubJoin, config_dir: Path) -> int:
    """Run ``potluck join`` against the stub and return its exit status."""
    return main([
        "join", "--server", stub.url, "--invite", INVITE,
        "--config-dir", str(config_dir),
    ])


def test_join_prints_an_issued_key_once_and_stores_no_copy(
    stub_join: _StubJoin,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The key reaches the screen on its own line, and reaches no file.

    On its own line so that handing it to a tool is a supported one-liner rather
    than something a person has to cut out of prose. Printed once, because a
    terminal a user scrolls back through must not become a place a credential
    accumulates. And stored nowhere: the identity directory ends up holding
    exactly the two files a join has always written.
    """
    stub_join.issue_key(API_KEY)
    config_dir = tmp_path / "identity"

    assert _join(stub_join, config_dir) == 0
    out = capsys.readouterr().out

    assert [line for line in out.splitlines() if line.startswith("ak-")] == [API_KEY]
    assert out.count(API_KEY) == 1
    # The endpoint the key is for travels with it, and so does the fact that the
    # server kept only a digest: a key that will not be shown again has to be
    # stored by whoever sees it, and this block is what tells them so.
    assert f"{stub_join.url}/openai/v1" in out
    assert "shown once; store it now" in out
    assert sorted(path.name for path in config_dir.iterdir()) == ["config.json", "key.json"]
    for path in config_dir.iterdir():
        assert API_KEY not in path.read_text(encoding="utf-8")
    # The identity saved beside it is the one the server derived.
    config_document = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
    assert config_document["subject_id"] == SUBJECT


def test_join_sends_its_own_public_key_and_no_credential(
    stub_join: _StubJoin,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """What the client puts on the wire is unchanged by what comes back on it.

    The request a key-issuing join sends is the request any join sends: the
    public key this machine generated, and nothing anybody could read as a
    credential. A key is an answer, never an input.
    """
    stub_join.issue_key(API_KEY)
    config_dir = tmp_path / "identity"

    assert _join(stub_join, config_dir) == 0
    capsys.readouterr()

    sent = stub_join.bodies[0].decode("utf-8")
    body = json.loads(sent)
    assert set(body) == {"invite_code", "identity", "device"}
    assert body["invite_code"] == INVITE
    assert set(body["identity"]) == {"public_key_b64", "display_name"}
    assert body["device"] is None
    key_document = json.loads((config_dir / "key.json").read_text(encoding="utf-8"))
    assert body["identity"]["public_key_b64"] == key_document["public_key_b64"]
    assert key_document["private_key_b64"] not in sent


def test_join_without_an_issued_key_prints_no_key_block(
    stub_join: _StubJoin,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An invite that issued no key leaves `join`'s output exactly as it was."""
    config_dir = tmp_path / "identity"

    assert _join(stub_join, config_dir) == 0
    out = capsys.readouterr().out

    assert "ak-" not in out
    assert "openai/v1" not in out
    assert "shown once; store it now" not in out
    assert f"joined as {SUBJECT}" in out
    assert sorted(path.name for path in config_dir.iterdir()) == ["config.json", "key.json"]


def test_a_rejoin_that_carries_no_key_prints_no_key_block(
    stub_join: _StubJoin,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A re-join is answered from the identity, so it re-shows no credential.

    The server cannot reproduce a plaintext it kept only a digest of, and an
    idempotent re-join is the common way to run into that. The second run must
    not claim a key the first one already showed, and must not print an empty
    block where one would have been.
    """
    stub_join.issue_key(API_KEY)
    config_dir = tmp_path / "identity"
    assert _join(stub_join, config_dir) == 0
    capsys.readouterr()

    # The idempotent answer: the identity again, and no secret with it.
    del stub_join.document["api_key"]
    assert _join(stub_join, config_dir) == 0
    out = capsys.readouterr().out

    assert "ak-" not in out
    assert "shown once; store it now" not in out
    assert f"joined as {SUBJECT}" in out


def test_the_key_wording_names_the_endpoint_and_can_hold_no_credential() -> None:
    """One spelling for the block, and no way for it to carry the key itself.

    `ux` is safe to embed anywhere because none of its strings can contain a
    credential, which is why this function takes the endpoint and not the key: a
    wording module that accepted a secret could render one into a diagnostic.
    """
    text = ux.joined_api_key_text(base_url="https://server.example:8443/openai/v1")

    assert "https://server.example:8443/openai/v1" in text
    assert "shown once; store it now" in text
    # Revoking the key is not revoking the member, and this is where a user
    # learns that.
    assert "api-key-revoke" in text
    assert "identity" in text
