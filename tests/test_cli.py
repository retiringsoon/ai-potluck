"""The ``potluck`` console entry point: offline tools and pre-network refusals.

These tests drive ``main()`` rather than the private command helpers, so the
argument wiring, the stdout/stderr split and the exit codes are exercised the
same way the installed ``potluck`` script uses them. The consumer network
commands that need a running server live in the ``ai-potluck-server``
repository's suite, beside the server they drive against.

:func:`aipotluck.cli.build_signed_request` is tested here even though it belongs
to the consumer path: minting a request needs no server, and it is the one seam
``ask`` and ``serve-openai`` share, so what it signs is a question this suite can
answer offline.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from aipotluck.canonical import base64_decode, verify_payload
from aipotluck.cli import (
    NoAvailableNodes,
    _await_terminal,
    _model_nodes,
    build_signed_request,
    load_joined_identity,
    main,
)
from aipotluck.errors import PotluckError, ProtocolError
from aipotluck.identity import generate_key_pair
from aipotluck.records import DOMAIN_AUTHORIZATION
from aipotluck.wire import ChatMessage, compute_request_digest, encode_chat_parameters

DOMAIN = "AIPotluck/test/v1"

MODEL = "coding-pool"
CONSUMER_TOKEN = "consumer-token-cli"
REQUEST_ID = "R-cli-1"

#: The URL key a pre-rename identity config carried. Spelled literally so the
#: test pins the key a real stale ``config.json`` holds; see the client
#: package's `RENAMED_CONFIG_KEYS`.
STALE_URL_KEY = "center_url"


def _keygen(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[Path, str]:
    """Run ``keygen`` and return the private key path and the printed public key."""
    out = tmp_path / "signing.key"
    assert main(["keygen", "--out", str(out)]) == 0
    public_key = capsys.readouterr().out.strip()
    assert public_key, "keygen must print the public key to stdout"
    return out, public_key


def _sign_args(key_path: Path, payload: Path) -> list[str]:
    """Build a ``sign`` invocation for the shared test domain."""
    return ["sign", "--key", str(key_path), "--domain", DOMAIN, "--payload", str(payload)]


def _verify_args(public_key: str, payload: Path, signature: str) -> list[str]:
    """Build a ``verify`` invocation for the shared test domain."""
    return [
        "verify",
        "--public-key",
        public_key,
        "--domain",
        DOMAIN,
        "--payload",
        str(payload),
        "--signature",
        signature,
    ]


def _write_payload(tmp_path: Path, name: str, value: object) -> Path:
    """Write ``value`` as JSON to ``tmp_path/name`` and return the path."""
    path = tmp_path / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_keygen_writes_private_key_with_0600_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_path, public_key = _keygen(tmp_path, capsys)

    assert key_path.exists()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert key_path.read_text(encoding="utf-8").strip()
    assert public_key


def test_keygen_refuses_to_clobber_without_force_and_succeeds_with_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_path, _ = _keygen(tmp_path, capsys)
    original = key_path.read_text(encoding="utf-8")

    assert main(["keygen", "--out", str(key_path)]) == 1
    err = capsys.readouterr().err
    assert "--force" in err
    assert key_path.read_text(encoding="utf-8") == original

    assert main(["keygen", "--out", str(key_path), "--force"]) == 0
    assert key_path.read_text(encoding="utf-8").strip()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_keygen_force_never_writes_the_key_into_a_world_readable_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``keygen --force`` must reach mode 0600 before any key byte is written.

    Regression for the ``O_TRUNC`` reuse race: the kernel ignores ``os.open``'s
    mode argument for a pre-existing inode, so a target left at 0644 used to
    receive the whole fresh private key and was only tightened by a trailing
    ``chmod``. Checking the final mode cannot see that window, so this test
    interposes ``os.write`` and samples the open descriptor's mode at the moment
    the secret bytes are handed to the kernel. A keygen that writes first fails
    with the recorded mode 0o644; one that sets the mode on the descriptor first
    records 0o600. The test also pins the end state: exactly the intended key
    contents, and no leftover file in the target directory.
    """
    from aipotluck.canonical import base64_encode
    from aipotluck.identity import load_private_key

    target = tmp_path / "signing.key"
    target.write_text("previous key material\n", encoding="utf-8")
    target.chmod(0o644)

    modes_sampled_at_write: list[int] = []
    real_write = os.write

    def spying_write(fd: int, data: bytes) -> int:
        modes_sampled_at_write.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", spying_write)

    assert main(["keygen", "--out", str(target), "--force"]) == 0
    public_key = capsys.readouterr().out.strip()

    # The key must not have been written while the file was group/world-readable.
    assert modes_sampled_at_write, "keygen must write the private key through os.write"
    observed = {oct(mode) for mode in modes_sampled_at_write}
    assert set(modes_sampled_at_write) == {0o600}, (
        f"private key was written while the file mode was {sorted(observed)}"
    )

    # The end state is exactly one 0600 file holding the matching private key.
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [entry.name for entry in tmp_path.iterdir()] == ["signing.key"]
    stored = target.read_text(encoding="utf-8")
    assert stored.endswith("\n")
    assert stored.strip() != "previous key material"
    private_key = load_private_key(stored.strip())
    derived_public_key = base64_encode(private_key.public_key().public_bytes_raw())
    assert derived_public_key == public_key


def test_sign_verify_round_trip_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_path, public_key = _keygen(tmp_path, capsys)
    payload_path = _write_payload(tmp_path, "payload.json", {"session": "s-1", "amount": "1000"})

    assert main(_sign_args(key_path, payload_path)) == 0
    signature = capsys.readouterr().out.strip()
    assert signature

    assert main(_verify_args(public_key, payload_path, signature)) == 0
    assert "valid" in capsys.readouterr().out.lower()


def test_verify_fails_for_tampered_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_path, public_key = _keygen(tmp_path, capsys)
    payload_path = _write_payload(tmp_path, "payload.json", {"session": "s-1", "amount": "1000"})

    assert main(_sign_args(key_path, payload_path)) == 0
    signature = capsys.readouterr().out.strip()

    tampered = _write_payload(tmp_path, "tampered.json", {"session": "s-1", "amount": "9999"})
    code = main(_verify_args(public_key, tampered, signature))

    assert code != 0
    assert capsys.readouterr().err.strip()


def test_sign_rejects_float_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_path, _ = _keygen(tmp_path, capsys)
    payload_path = _write_payload(tmp_path, "float.json", {"amount": 1.5})

    code = main(_sign_args(key_path, payload_path))
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out.strip() == ""
    assert "error" in captured.err.lower()
    assert "fractional" in captured.err.lower()


def test_verify_rejects_malformed_signature_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _key_path, public_key = _keygen(tmp_path, capsys)
    payload_path = _write_payload(tmp_path, "payload.json", {"a": 1})

    code = main(_verify_args(public_key, payload_path, "!!!not-base64!!!"))
    captured = capsys.readouterr()

    assert code == 1
    assert "base64" in captured.err.lower()


def _request_argv(consumer_config: Path, auth_file: Path | None, **overrides: Any) -> list[str]:
    """Build a ``request`` invocation with the shared fixtures' values."""
    argv = [
        "request",
        "--config",
        str(consumer_config),
        "--prompt",
        "write a haiku about sockets",
        "--model",
        MODEL,
        "--request-id",
        REQUEST_ID,
        "--idempotency-key",
        "idem-cli-1",
        "--max-charge",
        "20000",
    ]
    if auth_file is not None:
        argv += ["--auth-file", str(auth_file)]
    argv += overrides.pop("extra", [])
    return argv


def test_request_without_auth_file_fails_with_actionable_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure happens before any network traffic and names the remedy."""
    consumer_config = tmp_path / "consumer.json"
    consumer_config.write_text(
        json.dumps({"server_url": "http://127.0.0.1:1", "token": CONSUMER_TOKEN}),
        encoding="utf-8",
    )
    code = main(_request_argv(consumer_config, None))
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    message = captured.err.lower()
    assert "--auth-file" in message
    assert "potluck sign" in message
    assert "consumer key" in message


def test_ask_refuses_an_identity_config_still_naming_the_old_url_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An identity saved before the rename is told which key to write now.

    The refusal happens before any network traffic, and it names both the key
    the file has and the key that replaced it: a bare "required field missing"
    would send the operator looking for something absent rather than something
    stale.
    """
    directory = tmp_path / "aipotluck"
    directory.mkdir()
    public_key_b64, private_key_b64 = generate_key_pair()
    (directory / "config.json").write_text(
        json.dumps(
            {
                STALE_URL_KEY: "http://127.0.0.1:1",
                "key_id": "consumer-key-1",
                "subject_id": "consumer-X",
                "tenant_id": "tenant-1",
                "role": "consumer",
            }
        ),
        encoding="utf-8",
    )
    (directory / "key.json").write_text(
        json.dumps({"public_key_b64": public_key_b64, "private_key_b64": private_key_b64}),
        encoding="utf-8",
    )

    code = main(["ask", "--config-dir", str(directory), "hello"])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert STALE_URL_KEY in captured.err
    assert "renamed to 'server_url'" in captured.err
    assert private_key_b64 not in captured.err




def _public_key(public_key_b64: str) -> Ed25519PublicKey:
    """Load the public key half of a generated pair."""
    return Ed25519PublicKey.from_public_bytes(base64_decode(public_key_b64))


def _identity_documents() -> tuple[dict[str, Any], dict[str, Any], str]:
    """Return a saved identity's two documents and the matching public key.

    Shaped exactly as ``join`` leaves them on disk, so
    :func:`aipotluck.cli.build_signed_request` reads the fields it reads in
    production rather than a test-only shortcut.
    """
    public_key_b64, private_key_b64 = generate_key_pair()
    config_document = {
        "server_url": "http://127.0.0.1:1",
        "key_id": "consumer-key-1",
        "subject_id": "consumer-X",
        "tenant_id": "tenant-1",
        "role": "consumer",
    }
    key_document = {
        "public_key_b64": public_key_b64,
        "private_key_b64": private_key_b64,
    }
    return config_document, key_document, public_key_b64


def _signed(
    config_document: dict[str, Any],
    key_document: dict[str, Any],
    *,
    messages: tuple[ChatMessage, ...],
    parameters: Mapping[str, Any],
    delivery_mode: str = "buffered_complete",
) -> tuple[str, dict[str, Any]]:
    """Build one signed request with the shared test identity."""
    return build_signed_request(
        config_document=config_document,
        key_document=key_document,
        messages=messages,
        model=MODEL,
        parameters=parameters,
        max_charge=20_000,
        delivery_mode=delivery_mode,
        deadline_ms=60_000,
    )


def test_build_signed_request_submits_the_conversation_and_parameters_it_signs() -> None:
    """Every turn and every forwarded parameter is in the body *and* the digest.

    The server re-derives the digest from what was submitted and refuses a
    mismatch, so a body field the signature does not cover is a field a caller
    could change after signing. Asserting the digest equals one recomputed from
    the same values is what pins the two together.
    """
    config_document, key_document, public_key_b64 = _identity_documents()
    messages = (
        ChatMessage(role="system", content="answer in one word"),
        ChatMessage(role="user", content="what settles a request?"),
        ChatMessage(role="assistant", content="a receipt"),
        ChatMessage(role="user", content="and what backs the receipt?"),
    )
    parameters = encode_chat_parameters({"max_tokens": 64, "seed": 7, "stop": ["END"]})

    request_id, body = _signed(
        config_document, key_document, messages=messages, parameters=parameters
    )

    assert body["messages"] == [message.to_payload() for message in messages], (
        "the conversation reaches the server turn by turn, not as one prompt"
    )
    assert body["parameters"] == {"max_tokens": 64, "seed": 7, "stop": ["END"]}
    assert body["model"] == MODEL
    assert body["delivery_mode"] == "buffered_complete"
    authorization = body["authorization"]
    payload = authorization["payload"]
    assert payload["request_id"] == request_id
    assert payload["consumer_id"] == "consumer-X"
    assert payload["payer_account_id"] == "consumer-X"
    assert payload["request_digest"] == compute_request_digest(
        messages=messages,
        model=MODEL,
        delivery_mode="buffered_complete",
        parameters=parameters,
    )
    assert verify_payload(
        DOMAIN_AUTHORIZATION, payload, authorization["signature"],
        _public_key(public_key_b64),
    ), "the authorization the body carries is a real signature over that payload"


def test_a_different_parameter_set_signs_a_different_request() -> None:
    """The ceiling is bound to the parameters, not only to the conversation.

    An authorization signed for a 64-token answer must not be re-pointable at a
    4096-token one, so the two digests have to differ.
    """
    config_document, key_document, _ = _identity_documents()
    messages = (ChatMessage(role="user", content="what settles a request?"),)

    _, small = _signed(
        config_document, key_document, messages=messages,
        parameters=encode_chat_parameters({"max_tokens": 64}),
    )
    _, large = _signed(
        config_document, key_document, messages=messages,
        parameters=encode_chat_parameters({"max_tokens": 4096}),
    )

    assert small["authorization"]["payload"]["request_digest"] != (
        large["authorization"]["payload"]["request_digest"]
    )


def test_a_session_travels_in_the_body_and_stays_out_of_the_signature() -> None:
    """One session names a run; the digest still covers only the work.

    Both halves matter. The body has to carry the identifier or the server cannot
    attribute the turn, and the digest has to be *unchanged* by it or a caller
    would have to re-sign to move a turn between its own sessions.
    """
    config_document, key_document, _ = _identity_documents()
    messages = (ChatMessage(role="user", content="what settles a request?"),)
    parameters = encode_chat_parameters({"max_tokens": 64})

    _, with_session = build_signed_request(
        config_document=config_document,
        key_document=key_document,
        messages=messages,
        model=MODEL,
        parameters=parameters,
        max_charge=20_000,
        delivery_mode="buffered_complete",
        session_id="sess-agent-run-1",
    )
    _, without_session = _signed(
        config_document, key_document, messages=messages, parameters=parameters
    )

    assert with_session["session_id"] == "sess-agent-run-1"
    assert "session_id" not in without_session, (
        "a request that named no session must not carry an empty one"
    )
    assert with_session["authorization"]["payload"]["request_digest"] == (
        without_session["authorization"]["payload"]["request_digest"]
    )


def test_a_session_identifier_the_wire_refuses_is_refused_before_signing() -> None:
    """A bad identifier fails locally rather than at the server's boundary."""
    config_document, key_document, _ = _identity_documents()

    with pytest.raises(ProtocolError):
        build_signed_request(
            config_document=config_document,
            key_document=key_document,
            messages=(ChatMessage(role="user", content="hi"),),
            model=MODEL,
            parameters={},
            max_charge=20_000,
            delivery_mode="buffered_complete",
            session_id="two words",
        )


def test_an_unknown_identity_config_field_is_refused(tmp_path: Path) -> None:
    """The saved identity config has an accepted set like every other config.

    A field a future version no longer reads — or one an editor added by
    hand — is named when the identity loads, rather than silently ignored
    while commands authenticate with a subset of what the file claims.
    """
    config_document, key_document, _ = _identity_documents()
    config_document["server"] = config_document.pop("server_url")
    directory = tmp_path / ".aipotluck"
    directory.mkdir()
    (directory / "config.json").write_text(json.dumps(config_document), encoding="utf-8")
    (directory / "key.json").write_text(json.dumps(key_document), encoding="utf-8")
    with pytest.raises(PotluckError) as excinfo:
        load_joined_identity(directory)
    message = str(excinfo.value)
    assert "'server'" in message
    assert "server_url" in message, "the accepted set is named"


# --------------------------------------------------------------------------- #
# The node check during a wait
# --------------------------------------------------------------------------- #


class _StubClient:
    """A stand-in signed client whose reads come from a per-path script.

    `_await_terminal` polls two paths over and over, and a read that finds its
    queue empty fails the way an unreachable server would — the case the node
    check must never mistake for "there are no nodes".
    """

    def __init__(self, answers: dict[str, list[dict[str, Any]]]) -> None:
        self._answers = {path: list(items) for path, items in answers.items()}
        self.reads: list[str] = []

    def get(self, path: str) -> dict[str, Any]:
        self.reads.append(path)
        queue = self._answers.get(path)
        if not queue:
            raise PotluckError(f"stub client cannot read {path}")
        return queue.pop(0)


def test_an_unreadable_node_listing_is_never_read_as_no_nodes() -> None:
    """A listing that cannot be read propagates instead of flattening to none.

    The callers treat an empty listing as "no node can run this" and end the
    request over it — a request that may already be running and billing. An
    unreadable listing says nothing about whether a node exists, so it must
    raise and let the caller surface the real cause.
    """
    client = _StubClient({})

    with pytest.raises(PotluckError):
        _model_nodes(client, MODEL)


def test_a_malformed_node_listing_is_refused() -> None:
    """A `nodes` field that is not an array is a server contract violation."""
    client = _StubClient({"/v1/nodes": [{"nodes": "node-1"}]})

    with pytest.raises(PotluckError) as excinfo:
        _model_nodes(client, MODEL)

    assert "nodes" in str(excinfo.value)


def test_the_node_listing_is_filtered_to_sharing_nodes_of_the_model() -> None:
    """Only online, sharing nodes advertising the model can take the request."""
    wanted = {"model": MODEL, "share_enabled": True}
    client = _StubClient(
        {
            "/v1/nodes": [
                {
                    "nodes": [
                        wanted,
                        {"model": MODEL, "share_enabled": False},
                        {"model": "other", "share_enabled": True},
                        "not-a-node",
                    ]
                }
            ]
        }
    )

    assert _model_nodes(client, MODEL) == [wanted]


def test_a_running_request_is_never_ended_over_the_node_listing() -> None:
    """A node that pauses mid-flight is not a node that was never there.

    The request is RUNNING: a node demonstrably holds the attempt, so the work
    is billed whatever the listing says, and ending it here would cancel a real
    charge. The grace that ends an undispatchable request must not run.
    """
    client = _StubClient(
        {
            "/v1/requests/R-1": [
                {"status": "RUNNING"},
                {"status": "RUNNING"},
                {"status": "SUCCEEDED", "settlement": {"receipt_id": "R-1"}},
            ],
            "/v1/nodes": [{"nodes": []}, {"nodes": []}],
        }
    )

    document = _await_terminal(
        client, "R-1", model=MODEL, timeout_s=5.0, no_node_grace_s=0.05
    )

    assert document["status"] == "SUCCEEDED"


def test_a_routing_request_with_no_node_for_the_whole_grace_is_refused() -> None:
    """The positive half: a pre-dispatch request nothing can take ends early.

    Waiting out the full timeout would be a user staring at silence for a
    request that provably cannot start, so the empty listing ends it — with the
    refusal that says so, not a timeout that hides the cause.
    """
    client = _StubClient(
        {
            "/v1/requests/R-1": [{"status": "ROUTING"}, {"status": "ROUTING"}],
            "/v1/nodes": [{"nodes": []}, {"nodes": []}],
        }
    )

    with pytest.raises(NoAvailableNodes):
        _await_terminal(client, "R-1", model=MODEL, timeout_s=5.0, no_node_grace_s=0.05)
