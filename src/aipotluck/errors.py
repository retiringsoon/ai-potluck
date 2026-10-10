"""Domain exceptions for the AI resource sharing platform.

Every error carries an actionable message: the caller (operator, coordinator, or
connector) must be able to tell what invariant was violated and what to do next.
"""

from __future__ import annotations


class PotluckError(Exception):
    """Base class for every error raised by this package."""


class CanonicalizationError(PotluckError):
    """Raised when a payload cannot be canonicalized under strict RFC 8785 JCS.

    The platform refuses to sign or verify anything it cannot canonicalize
    deterministically, because two different byte strings for the same logical
    payload would break signature verification.
    """


class DuplicateKeyError(CanonicalizationError):
    """Raised when a JSON object contains the same key twice.

    RFC 8785 has no rule for duplicate keys; different parsers keep different
    ones. Accepting them would let a peer show us one document and sign
    another.
    """


class NonCanonicalNumberError(CanonicalizationError):
    """Raised for floats, exponents, or integers outside the interoperable range.

    Amounts must be carried as canonical decimal strings, so a JSON number in a
    signed payload is always a schema violation rather than a rounding question.
    """


class SignatureError(PotluckError):
    """Base class for signature and key-binding failures."""


class UnknownKeyError(SignatureError):
    """Raised when a payload references a key the verifier has never seen.

    A receipt must never be trusted because it carries its own public key.
    """


class KeyRevokedError(SignatureError):
    """Raised when a key was validly registered but has since been revoked."""


class DelegationError(SignatureError):
    """Raised when a signing key acted outside its delegated authority."""


class IdentityBindingError(SignatureError):
    """Raised when a key is not bound to the identity it claims to speak for."""


class MeteringError(PotluckError):
    """Base class for token metering failures."""


class CoverageError(MeteringError):
    """Raised when two measurement records claim overlapping coverage."""


class CounterResetError(MeteringError):
    """Raised when a cumulative counter moves backwards without a new epoch."""


class LedgerError(PotluckError):
    """Base class for accounting failures."""


class UnbalancedJournalError(LedgerError):
    """Raised when journal entries do not sum to zero in a single currency."""


class InsufficientFundsError(LedgerError):
    """Raised when a reservation or settlement would overdraw an account."""


class SettlementError(LedgerError):
    """Raised when a settlement transaction cannot be completed."""


class AllocationError(LedgerError):
    """Raised when an allocation contract is invalid or cannot be applied."""


class StateTransitionError(PotluckError):
    """Raised on an illegal state machine transition."""


class ConflictError(PotluckError):
    """Raised when a record ID is reused with different content.

    The platform never resolves a conflict by keeping the last writer; both
    versions are preserved and the record is frozen for review.
    """


class VerificationError(PotluckError):
    """Raised when offline verification cannot produce a trustworthy verdict."""


class ProtocolError(PotluckError):
    """Raised when a wire message is malformed, unknown, or out of order.

    A protocol violation is never silently ignored: an unknown message type, a
    non-strict JSON body, or a sequence regression all surface here so the peer
    can be told exactly what was wrong instead of having its message dropped.
    """


class GatewayError(PotluckError):
    """Raised for a center-gateway condition no narrower exception describes.

    Examples are an unknown receipt and a dispatch input that went missing,
    which are operational failures of the center rather than protocol
    violations by a peer.
    """