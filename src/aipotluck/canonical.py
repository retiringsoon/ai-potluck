"""Strict JSON canonicalization (RFC 8785 JCS) and domain-separated signing.

Two rules make this stricter than a normal JSON dump, and both are deliberate:

1. Floats are rejected outright, including the `NaN`, `Infinity` and `-Infinity`
   literals that are not JSON at all but that Python's `json` module accepts by
   default. Amounts, counters, and sequence numbers travel as canonical decimal
   strings, because IEEE-754 doubles cannot represent large integers exactly and
   cross-language re-serialization would drift.
2. Duplicate object keys are rejected. RFC 8785 does not define them, and two
   parsers may keep different halves of the same document.

Object keys are ordered by UTF-16 code unit, as RFC 8785 section 3.2.3 requires.
That differs from Python's default code-point ordering for non-BMP keys: `U+1F600`
encodes to the surrogate `D83D`, so it sorts *before* `U+E000` in UTF-16 while
sorting after it by code point.

`sign_payload` prefixes a domain string and a NUL byte before the canonical
bytes, so a signature produced for one record type can never be replayed as a
signature for another.
"""

from __future__ import annotations

import json
from typing import Any, Final

from .errors import (
    CanonicalizationError,
    DuplicateKeyError,
    NonCanonicalNumberError,
    SignatureError,
)

# JSON numbers are only interoperable as integers in this range. Larger values
# would need the ES6 number-to-string algorithm across languages, which is not
# worth the risk for a settlement system.
MAX_SAFE_INTEGER: Final = 2**53 - 1
MIN_SAFE_INTEGER: Final = -(2**53 - 1)

_ESCAPES: Final = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Object hook that refuses duplicate keys instead of silently keeping one."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def loads_strict(text: str | bytes) -> Any:
    """Parse JSON, rejecting duplicate keys and non-integer numbers.

    The three non-numeric literals `NaN`, `Infinity` and `-Infinity` are rejected
    as well. They are extensions that `json.loads` routes to `parse_constant`
    rather than `parse_float`, so hooking only `parse_float` would let them
    through and produce a document no canonical encoder can round-trip.

    Args:
        text: The JSON document as `str` or UTF-8 `bytes`.

    Returns:
        The parsed value.

    Raises:
        DuplicateKeyError: If any object repeats a key.
        NonCanonicalNumberError: If a number is fractional or out of range, or if
            a `NaN`, `Infinity` or `-Infinity` literal appears.
        CanonicalizationError: If the bytes are not valid UTF-8 JSON.
    """

    def reject_float(raw: str) -> Any:
        raise NonCanonicalNumberError(
            f"fractional number {raw!r} is not allowed; carry amounts as decimal strings"
        )

    def reject_constant(raw: str) -> Any:
        raise NonCanonicalNumberError(
            f"{raw} is not valid JSON and cannot be canonicalized; "
            "carry amounts as decimal strings"
        )

    def check_int(raw: str) -> int:
        value = int(raw)
        if value > MAX_SAFE_INTEGER or value < MIN_SAFE_INTEGER:
            raise NonCanonicalNumberError(
                f"integer {raw!r} exceeds the interoperable safe range "
                f"[{MIN_SAFE_INTEGER}, {MAX_SAFE_INTEGER}]; carry large values as strings"
            )
        return value

    try:
        return json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_float=reject_float,
            parse_int=check_int,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise CanonicalizationError(f"invalid JSON: {exc}") from exc


def _serialize_string(value: str) -> str:
    """Serialize a JSON string per RFC 8785: minimal escaping, raw UTF-8 otherwise."""
    out = ['"']
    for char in value:
        code = ord(char)
        escape = _ESCAPES.get(code)
        if escape is not None:
            out.append(escape)
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _utf16_key(key: str) -> bytes:
    """Return the sort key that puts `key` in RFC 8785 UTF-16 code-unit order.

    RFC 8785 section 3.2.3 orders members by their UTF-16 code units, which is
    what JavaScript's own property ordering uses. Python compares strings by code
    point, and the two disagree once a non-BMP character is involved: `U+1F600`
    is a surrogate pair starting at `D83D`, so UTF-16 places it before `U+E000`
    while code-point order places it after. Comparing the big-endian UTF-16 bytes
    reproduces the required order, because byte-wise comparison of UTF-16-BE is
    the same as comparing code-unit sequences.

    Args:
        key: The object key to build a sort key for.

    Returns:
        The UTF-16 big-endian encoding, suitable for `sorted` as a key.
    """
    return key.encode("utf-16-be")


def _serialize(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _serialize_string(value)
    if isinstance(value, int):
        if value > MAX_SAFE_INTEGER or value < MIN_SAFE_INTEGER:
            raise NonCanonicalNumberError(
                f"integer {value} exceeds the interoperable safe range; use a decimal string"
            )
        return str(value)
    if isinstance(value, float):
        raise NonCanonicalNumberError(
            "floats cannot be canonicalized safely; carry amounts as decimal strings"
        )
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_serialize(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = []
        for key in sorted(value, key=_utf16_key):
            if not isinstance(key, str):
                raise CanonicalizationError(f"object keys must be strings, got {type(key)!r}")
            parts.append(_serialize_string(key) + ":" + _serialize(value[key]))
        return "{" + ",".join(parts) + "}"
    raise CanonicalizationError(f"value of type {type(value)!r} cannot be canonicalized")


def canonicalize(value: Any) -> bytes:
    """Return the RFC 8785 canonical UTF-8 encoding of a JSON value.

    Object members are ordered by UTF-16 code unit rather than by code point,
    which only differs for non-BMP keys but must match the specification for
    signatures taken over canonical bytes to interoperate.

    Args:
        value: A value composed of dicts, lists, strings, integers, bools and None.

    Returns:
        The canonical UTF-8 bytes.

    Raises:
        CanonicalizationError: If the value contains a non-canonical type.
    """
    return _serialize(value).encode("utf-8")


def canonical_digest(value: Any) -> str:
    """Return the hex SHA-256 digest of the canonical encoding.

    Args:
        value: The payload to digest.

    Returns:
        Lowercase hex digest, prefixed with ``sha256:``.
    """
    import hashlib

    return "sha256:" + hashlib.sha256(canonicalize(value)).hexdigest()


def encode_signed_payload(domain: str, payload: dict[str, Any]) -> bytes:
    """Build the exact bytes that get signed for a given record type.

    The domain prefix and the NUL separator stop a signature over, say, a
    settlement receipt from ever validating as a signature over a usage report.

    Args:
        domain: The domain string, e.g. ``"AIPotluck/settlement/v1"``.
        payload: The record payload.

    Returns:
        ``domain + b"\\0" + canonical_bytes``.

    Raises:
        CanonicalizationError: If the domain contains a NUL byte.
    """
    if "\0" in domain:
        raise CanonicalizationError("domain must not contain a NUL byte")
    return domain.encode("utf-8") + b"\0" + canonicalize(payload)


def sign_payload(domain: str, payload: dict[str, Any], private_key: Any) -> str:
    """Sign a payload under a domain prefix.

    Args:
        domain: Domain separation string.
        payload: The record payload.
        private_key: An Ed25519 private key object.

    Returns:
        Base64 signature, without the domain prefix (it is implied by `domain`).

    Raises:
        SignatureError: If the key is not an Ed25519 private key.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if not isinstance(private_key, Ed25519PrivateKey):
        raise SignatureError(f"expected an Ed25519 private key, got {type(private_key)!r}")
    return base64_encode(private_key.sign(encode_signed_payload(domain, payload)))


def verify_payload(
    domain: str, payload: dict[str, Any], signature_b64: str, public_key: Any
) -> bool:
    """Verify a domain-separated signature.

    Args:
        domain: The domain prefix used when signing.
        payload: The record payload as received.
        signature_b64: Base64 signature from `sign_payload`.
        public_key: An Ed25519 public key object.

    Returns:
        True when the signature matches.

    Raises:
        SignatureError: If the key is not an Ed25519 public key or the
            signature is not valid base64.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not isinstance(public_key, Ed25519PublicKey):
        raise SignatureError(f"expected an Ed25519 public key, got {type(public_key)!r}")
    try:
        signature = base64_decode(signature_b64)
    except ValueError as exc:
        raise SignatureError(f"signature is not valid base64: {exc}") from exc
    try:
        public_key.verify(signature, encode_signed_payload(domain, payload))
    except InvalidSignature:
        return False
    return True


def base64_encode(data: bytes) -> str:
    """Encode bytes as standard base64 text."""
    import base64

    return base64.b64encode(data).decode("ascii")


def base64_decode(text: str) -> bytes:
    """Decode standard base64 text, raising SignatureError on malformed input."""
    import base64
    import binascii

    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SignatureError(f"invalid base64: {exc}") from exc


def encode_bytes(data: bytes) -> str:
    """Encode raw bytes (for example an Ed25519 public key) as base64 text."""
    return base64_encode(data)


def decode_bytes(text: str) -> bytes:
    """Decode base64 text into raw bytes."""
    return base64_decode(text)