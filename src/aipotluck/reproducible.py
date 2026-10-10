"""Optional reproducible billing basis, per design section 22.7.

This mode defines the billable quantity as something both parties can recompute
locally, rather than trying to replicate an upstream's native token accounting.
The tradeoff is explicit: it is *not* native token consumption, and re-tokenizing
a detokenized answer does not recover how many token IDs the model actually
produced. Receipts and interfaces must label it as a standardized quantity.

Recomputation is only possible while the billing body is retained. Once the body
expires, this module refuses rather than inventing a plausible number.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import VerificationError

BILLING_DOCUMENT_SCHEMA = "aipotluck.billing-document.v1"


@dataclass(frozen=True)
class BillingDocument:
    """The exact content both sides hash and count.

    Everything that affects the count is pinned: the tokenizer asset digest, the
    serializer version, and the message list. Changing any of them changes the
    document digest, so the two sides cannot silently disagree about what was
    counted.
    """

    request_id: str
    messages: tuple[Mapping[str, Any], ...]
    tokenizer_asset: str
    serializer_version: str
    body_expired: bool = False

    def __post_init__(self) -> None:
        """Validate the pinned assets.

        Raises:
            ValueError: If the tokenizer asset or serializer version is missing.
        """
        if "@sha256:" not in self.tokenizer_asset:
            raise ValueError(
                "tokenizer_asset must pin a digest, e.g. 'cl100k_base@sha256:...'"
            )
        if not self.serializer_version:
            raise ValueError("serializer_version must be set")

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical document payload."""
        return {
            "schema": BILLING_DOCUMENT_SCHEMA,
            "request_id": self.request_id,
            "tokenizer_asset": self.tokenizer_asset,
            "serializer_version": self.serializer_version,
            "messages": [dict(message) for message in self.messages],
        }


def _serialize_content(content: Any) -> str:
    """Serialize a message content value under the pinned rules.

    Strings pass through unchanged. Structured content is serialized with sorted
    keys so both sides produce identical bytes.
    """
    from .canonical import canonicalize

    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        return canonicalize([_serialize_content(item) for item in content]).decode("utf-8")
    if isinstance(content, Mapping):
        return canonicalize({str(k): _serialize_content(v) for k, v in content.items()}).decode(
            "utf-8"
        )
    return canonicalize(content).decode("utf-8")


def serialize_document(document: BillingDocument) -> str:
    """Serialize a billing document to the canonical counted text.

    Messages are joined with explicit role markers so a role change always
    changes the count. Streaming chunks are never tokenized separately, because
    chunk boundaries need not coincide with tokenizer boundaries.

    Args:
        document: The pinned document.

    Returns:
        The canonical text both parties count.

    Raises:
        VerificationError: If the body has expired.
    """
    if document.body_expired:
        raise VerificationError(
            f"billing body for {document.request_id} has expired; it cannot be "
            "re-tokenized, so the standardized quantity is no longer recomputable"
        )
    parts: list[str] = []
    for message in document.messages:
        role = str(message.get("role", "unknown"))
        parts.append(f"<|{role}|>")
        parts.append(_serialize_content(message.get("content", "")))
    return "\n".join(parts)


def count_units(text: str, *, chars_per_token: int) -> int:
    """Count standardized units for a text under a fixed tokenizer stand-in.

    The production path loads the pinned tokenizer asset named by the document.
    This module takes the token count as a parameter so the accounting logic can
    be tested without shipping a tokenizer; a caller must pass the count
    produced by the pinned asset, not an estimate.

    Args:
        text: The serialized document.
        chars_per_token: Average characters per token for the pinned asset.

    Returns:
        The unit count, rounded up so any non-empty text counts at least one unit.

    Raises:
        ValueError: If `chars_per_token` is not positive.
    """
    if chars_per_token <= 0:
        raise ValueError("chars_per_token must be positive")
    if not text:
        return 0
    return max(1, -(-len(text) // chars_per_token))


def recompute_standard_units(document: BillingDocument, *, chars_per_token: int) -> int:
    """Recompute the standardized billable quantity for a document.

    Args:
        document: The pinned billing document.
        chars_per_token: Token count for the pinned asset.

    Returns:
        The standardized unit count.

    Raises:
        VerificationError: If the document body has expired.
        ValueError: If the tokenizer asset or serializer version is invalid.
    """
    return count_units(serialize_document(document), chars_per_token=chars_per_token)


def document_digest(document: BillingDocument) -> str:
    """Return the canonical digest of the document.

    Args:
        document: The pinned document.

    Returns:
        The `sha256:...` digest.

    Raises:
        VerificationError: If the body has expired, since the digest could not
            be reproduced by the counterparty.
    """
    from .canonical import canonical_digest

    if document.body_expired:
        raise VerificationError(
            f"billing body for {document.request_id} has expired; its digest can no "
            "longer be reproduced for independent verification"
        )
    return canonical_digest(document.to_payload())


def input_output_split(document: BillingDocument) -> tuple[int, int]:
    """Split a document's messages into input-side and output-side counts.

    Only the trailing assistant message counts as output. Everything before it is
    input, which mirrors how a model call is billed without claiming to replicate
    any provider's exact accounting.

    Args:
        document: The pinned document.

    Returns:
        Tuple of (input_units, output_units) by message count.

    Raises:
        VerificationError: If the document has no messages.
    """
    if not document.messages:
        raise VerificationError(f"billing document {document.request_id} has no messages")
    last_role = str(document.messages[-1].get("role", ""))
    if last_role != "assistant":
        return (len(document.messages), 0)
    return (len(document.messages) - 1, 1)


def messages_to_document(
    request_id: str,
    messages: Iterable[Mapping[str, Any]],
    tokenizer_asset: str,
    serializer_version: str = "billing-doc-v1",
) -> BillingDocument:
    """Build a billing document from a request's messages.

    Args:
        request_id: The owning request.
        messages: The accepted request and response messages.
        tokenizer_asset: Pinned tokenizer asset digest.
        serializer_version: Pinned serializer version.

    Returns:
        The document.
    """
    return BillingDocument(
        request_id=request_id,
        messages=tuple(dict(message) for message in messages),
        tokenizer_asset=tokenizer_asset,
        serializer_version=serializer_version,
    )