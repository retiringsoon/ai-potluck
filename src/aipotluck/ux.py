"""Human-facing wording for machine states, kept in exactly one place.

Why this module exists: the same internal token — ``RECOVERING``, ``EXPIRED``,
``insufficient_funds`` — has to be explained on three surfaces (``ask``,
``doctor``, the contributor wizard). Spelling the phrase out at each call site
is how two screens end up disagreeing about what a state means, and how a state
added to the machine later silently renders as its wire token. So every phrase
and every "what do I do now" line lives here, and a unit test asserts the maps
are *total* over the enums: adding a state without a phrase fails the suite
instead of shipping a raw ``OUTCOME_UNKNOWN`` at an operator.

Two rules the call sites rely on and this module enforces by construction:

* An unknown state is never silently rendered as itself; it degrades to a
  clearly-marked "state needs confirmation" phrase. A half-understood wire token
  is worse than an honest "state to be confirmed", because the token looks
  authoritative.
* An unknown *usage* is never rendered as a number. `uncertain_usage_cost_text`
  exists so the CLI has one thing to call instead of inventing ``cost 0`` for a
  request whose meter never reported.

Nothing here reads a secret, a file, or the network. It is pure string mapping,
which is what makes it safe for ``doctor`` to embed: the redaction guarantee
holds because none of these strings can contain a credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

#: Request-status phrases, keyed by the wire token from
#: :mod:`aipotluck.statemachine`. The order mirrors the state machine so a reader
#: can diff the two by eye.
REQUEST_STATUS_PHRASES: Final[dict[str, str]] = {
    "CREATED": "accepted, preparing",
    "RESERVED": "credit reserved, preparing",
    "ROUTING": "looking for available nodes",
    "RUNNING": "generating",
    "RECOVERING": "recovering the previous request",
    "SUCCEEDED": "done",
    "FAILED": "did not finish",
    "CANCELLED": "cancelled",
    "EXPIRED": "timed out unfinished",
}

#: Attempt-status phrases. ``OFFERED``/``ACCEPTED``/``AUTHORIZED`` are all
#: "waiting" from the consumer's point of view — they are internal routing
#: bookkeeping, not things a user can act on — so they deliberately share one
#: phrase rather than exposing four near-identical tokens.
ATTEMPT_STATUS_PHRASES: Final[dict[str, str]] = {
    "PREPARED": "preparing",
    "OFFERED": "waiting",
    "ACCEPTED": "waiting",
    "AUTHORIZED": "waiting",
    "SUBMITTING": "generating",
    "RUNNING": "generating",
    "COMPLETED": "done",
    "FAILED": "did not finish",
    "CANCELLED": "cancelled",
    "OUTCOME_UNKNOWN": "usage to be confirmed",
    "REVOKED_BEFORE_SUBMIT": "revoked before submit",
}

#: Usage-status phrases. ``UNKNOWN``/``PARTIAL`` must never read as "free".
USAGE_STATUS_PHRASES: Final[dict[str, str]] = {
    "EMPTY": "no usage yet",
    "PROVISIONAL": "usage provisional",
    "COMPLETE": "usage confirmed",
    "PARTIAL": "usage to be confirmed",
    "UNKNOWN": "usage to be confirmed",
}

#: Rendered for any state this build does not recognise.
UNKNOWN_STATE_PHRASE: Final[str] = "state to be confirmed"

#: The one phrase for "the meter has not told us the cost yet".
UNCERTAIN_USAGE_PHRASE: Final[str] = "usage to be confirmed"

#: The cost line for a request whose usage cannot be determined. It carries a
#: next step, because "pending" without a remedy is just a nicer error.
UNCERTAIN_USAGE_COST_TEXT: Final[str] = (
    "cost: usage to be confirmed (the upstream reported no usage; "
    "check it against the receipt later)"
)

#: The bounded-wait message shown when the center has no node for the model.
NO_NODES_PHRASE: Final[str] = "no available nodes"
NO_NODES_NEXT_STEP: Final[str] = (
    "no available nodes: retry later or contact the operator; "
    "check that a contributing node is online and has not paused sharing "
    "(a contributor runs `potluck share status` on their own machine to see)"
)

#: The wording for an ``ask`` that could not even settle on a model, because no
#: contribution node was online to read one from. Deliberately distinct from
#: :data:`NO_NODES_PHRASE`: there a model was named and no node could run it,
#: here the client refuses to create a request at all rather than dispatch one
#: that provably cannot be routed. Naming what was *not* done is the part that
#: makes it actionable — an open-ended wait reads as a hung client.
NO_MODEL_PHRASE: Final[str] = "no available model"
NO_MODEL_NEXT_STEP: Final[str] = (
    "no available model: no contributing node is online to infer a model name "
    "from, so no request was created. Retry later, name one explicitly with "
    "`--model <model>`, or contact the operator to check that a contributing "
    "node is online and has not paused sharing "
    "(a contributor runs `potluck share status` on their own machine to see)"
)


def describe_request(status: str) -> str:
    """Return the human phrase for a request status token.

    Args:
        status: A token from ``RequestStatus``, or anything else the wire sent.

    Returns:
        The phrase, or :data:`UNKNOWN_STATE_PHRASE` when the token is not one
        this build knows. Deliberately a phrase rather than the raw token: an
        unknown token shown verbatim looks authoritative and hides the fact
        that this client cannot interpret it.
    """
    return REQUEST_STATUS_PHRASES.get(status, UNKNOWN_STATE_PHRASE)


def describe_attempt(status: str) -> str:
    """Return the human phrase for an attempt status token."""
    return ATTEMPT_STATUS_PHRASES.get(status, UNKNOWN_STATE_PHRASE)


def describe_usage(status: str) -> str:
    """Return the human phrase for a usage status token."""
    return USAGE_STATUS_PHRASES.get(status, UNKNOWN_STATE_PHRASE)


def uncertain_usage_cost_text(*, receipt_id: str | None = None) -> str:
    """Return the cost line to print when usage is not determinable.

    Callers must use this instead of formatting a charge: a request whose meter
    never reported must not be shown as ``cost 0``, because that is a false
    statement about money rather than a missing one.

    Args:
        receipt_id: The receipt the amount will eventually appear under, when
            the center issued one. Naming it turns "we do not know" into
            "here is where to check", which is the difference between a
            confusing silence and an actionable one.
    """
    if receipt_id:
        return (
            f"cost: {UNCERTAIN_USAGE_PHRASE} "
            f"(receipt {receipt_id}; the final amount can be checked there)"
        )
    return UNCERTAIN_USAGE_COST_TEXT


class FailureKind(StrEnum):
    """The failure families the CLI can tell apart and advise on."""

    CREDENTIALS = "credentials"
    QUOTA = "insufficient_funds"
    UPSTREAM_UNREACHABLE = "upstream_unreachable"
    CENTER_UNREACHABLE = "center_unreachable"
    NO_NODES = "no_nodes"
    TIMEOUT = "timeout"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FailureGuidance:
    """One failure family's phrase and its concrete next step."""

    kind: FailureKind
    phrase: str
    next_step: str


#: The three failure families the design calls out, each with a *different*
#: next step. A single "something went wrong" line is what makes a platform
#: feel unknowable; the whole point here is that the remedy differs by cause.
CREDENTIALS_GUIDANCE = FailureGuidance(
    kind=FailureKind.CREDENTIALS,
    phrase="credential no longer valid",
    next_step=(
        "credential no longer valid: re-run "
        "`potluck join --center <center url> --invite <new code>` for a fresh "
        "identity, or contact the operator to re-issue or unblock your key"
    ),
)
QUOTA_GUIDANCE = FailureGuidance(
    kind=FailureKind.QUOTA,
    phrase="insufficient credit",
    next_step=(
        "insufficient credit: contact the operator to add credit to your account "
        "(an operator grants it to a new member with "
        "`potluck-server admin invite --quota <milli-credits>`, or tops up an existing "
        "account on the center directly), then retry"
    ),
)
UPSTREAM_UNREACHABLE_GUIDANCE = FailureGuidance(
    kind=FailureKind.UPSTREAM_UNREACHABLE,
    phrase="upstream unreachable",
    next_step=(
        "upstream unreachable: check this machine's network and egress policy, "
        "and confirm the upstream endpoint, key and model are correct "
        "(a contributor reads the local upstream configuration with "
        "`potluck share status`, and re-runs `potluck share setup` if needed)"
    ),
)
CENTER_UNREACHABLE_GUIDANCE = FailureGuidance(
    kind=FailureKind.CENTER_UNREACHABLE,
    phrase="center unreachable",
    next_step=(
        "center unreachable: check the center address, this machine's network, "
        "and whether the center process is running"
    ),
)
NO_NODES_GUIDANCE = FailureGuidance(
    kind=FailureKind.NO_NODES,
    phrase=NO_NODES_PHRASE,
    next_step=NO_NODES_NEXT_STEP,
)
TIMEOUT_GUIDANCE = FailureGuidance(
    kind=FailureKind.TIMEOUT,
    phrase="timed out unfinished",
    next_step=(
        "timed out unfinished: pick it up later with `potluck ask --resume`, "
        "or check whether the node is busy"
    ),
)
CONFLICT_GUIDANCE = FailureGuidance(
    kind=FailureKind.CONFLICT,
    phrase="request refused",
    next_step=(
        "request refused (state conflict): usually means the request already "
        "reached a terminal state; read it with `potluck status`"
    ),
)
UNKNOWN_GUIDANCE = FailureGuidance(
    kind=FailureKind.UNKNOWN,
    phrase="unexpected failure",
    next_step=(
        "unexpected failure: export a redacted diagnostic with "
        "`potluck doctor --json` and report it to the operator"
    ),
)

#: Machine code (the gateway's ``error`` field, or one this CLI mints) to
#: guidance. Multiple codes may map to one family; the family, not the code, is
#: what the user reads.
_GUIDANCE_BY_CODE: Final[dict[str, FailureGuidance]] = {
    "forbidden": CREDENTIALS_GUIDANCE,
    "unauthorized": CREDENTIALS_GUIDANCE,
    "key_revoked": CREDENTIALS_GUIDANCE,
    "identity_binding": CREDENTIALS_GUIDANCE,
    "invalid_token": CREDENTIALS_GUIDANCE,
    "insufficient_funds": QUOTA_GUIDANCE,
    "upstream_unreachable": UPSTREAM_UNREACHABLE_GUIDANCE,
    "center_unreachable": CENTER_UNREACHABLE_GUIDANCE,
    "no_nodes": NO_NODES_GUIDANCE,
    "timeout": TIMEOUT_GUIDANCE,
    "conflict": CONFLICT_GUIDANCE,
}

#: HTTP status to guidance, used when the gateway gave no machine code but the
#: status is unambiguous.
_GUIDANCE_BY_HTTP_STATUS: Final[dict[int, FailureGuidance]] = {
    401: CREDENTIALS_GUIDANCE,
    402: QUOTA_GUIDANCE,
    403: CREDENTIALS_GUIDANCE,
    409: CONFLICT_GUIDANCE,
    502: UPSTREAM_UNREACHABLE_GUIDANCE,
    503: UPSTREAM_UNREACHABLE_GUIDANCE,
    504: UPSTREAM_UNREACHABLE_GUIDANCE,
}


def guidance_for(kind: FailureKind) -> FailureGuidance:
    """Return the guidance for a failure family."""
    for guidance in (
        CREDENTIALS_GUIDANCE,
        QUOTA_GUIDANCE,
        UPSTREAM_UNREACHABLE_GUIDANCE,
        CENTER_UNREACHABLE_GUIDANCE,
        NO_NODES_GUIDANCE,
        TIMEOUT_GUIDANCE,
        CONFLICT_GUIDANCE,
        UNKNOWN_GUIDANCE,
    ):
        if guidance.kind == kind:
            return guidance
    return UNKNOWN_GUIDANCE


def classify_failure(
    *,
    error_code: str | None = None,
    http_status: int | None = None,
    message: str = "",
) -> FailureGuidance:
    """Classify a failure into a family and its next step.

    Resolution order is deliberate: an explicit machine code beats an HTTP
    status, which beats a substring probe of the message. The code is a claim by
    the producer about *why*, and a status is only a coarse class, so preferring
    the code keeps a ``403`` that means "credential revoked" from being read the
    same as a ``403`` that means "not your tenant".

    Args:
        error_code: The gateway's machine code, or one this CLI minted.
        http_status: The HTTP status, when the failure came from a response.
        message: The operator-facing message, probed only as a last resort.

    Returns:
        The guidance whose phrase and next step the caller should show.
    """
    if error_code is not None:
        guidance = _GUIDANCE_BY_CODE.get(error_code)
        if guidance is not None:
            return guidance
    if http_status is not None:
        guidance = _GUIDANCE_BY_HTTP_STATUS.get(http_status)
        if guidance is not None:
            return guidance
    lowered = message.lower()
    if "could not reach the center" in lowered:
        return CENTER_UNREACHABLE_GUIDANCE
    if "did not reach a terminal state within" in lowered:
        return TIMEOUT_GUIDANCE
    if "unreachable" in lowered:
        return UPSTREAM_UNREACHABLE_GUIDANCE
    return UNKNOWN_GUIDANCE
