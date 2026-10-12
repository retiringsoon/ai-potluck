"""The ``potluck`` console entry point: offline tools and pre-network refusals.

These tests drive ``main()`` rather than the private command helpers, so the
argument wiring, the stdout/stderr split and the exit codes are exercised the
same way the installed ``potluck`` script uses them. The consumer network
commands that need a running center live in the ``ai-potluck-server``
repository's suite, beside the center they drive against.

:func:`aipotluck.cli.build_signed_request` is tested here even though it belongs
to the consumer path: minting a request needs no center, and it is the one seam
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
from aipotluck.cli import build_signed_request, main
from aipotluck.identity import generate_key_pair
from aipotluck.records import DOMAIN_AUTHORIZATION
from aipotluck.wire import ChatMessage, compute_request_digest, encode_chat_parameters

DOMAIN = "AIPotluck/test/v1"

MODEL = "coding-pool"
CONSUMER_TOKEN = "consumer-token-cli"
REQUEST_ID = "R-cli-1"


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
        json.dumps({"center_url": "http://127.0.0.1:1", "token": CONSUMER_TOKEN}),
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
        "center_url": "http://127.0.0.1:1",
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

    The center re-derives the digest from what was submitted and refuses a
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
        "the conversation reaches the center turn by turn, not as one prompt"
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
