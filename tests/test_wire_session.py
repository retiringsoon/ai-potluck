"""The session identifier the request model carries.

A session is a *label*, not a credential and not part of the work: it says which
run of an agent loop a request belongs to so the center can aggregate that run's
cost and prefer the node that served its last turn. Two properties are worth
pinning here, because both are easy to get subtly wrong:

* The identifier is refused at the boundary rather than stored. It travels in an
  HTTP header on the OpenAI-compatible surface and is printed by an operator
  command, so a value carrying a line break or a control character is one that
  could inject a header field or render as an unreadable listing.
* Minting is unguessable and one-per-call. A session id is caller-chosen on the
  signed path, so the one this package mints for a local endpoint must not be
  predictable by another process on the same machine.
"""

from __future__ import annotations

import pytest

from aipotluck.errors import ProtocolError
from aipotluck.wire import (
    MAX_SESSION_ID_CHARS,
    SESSION_HEADER,
    check_session_id,
    new_session_id,
)


def test_a_minted_session_is_prefixed_unique_and_accepted_by_its_own_check() -> None:
    """Two mints differ, both read as one identifier, and both pass the check."""
    first = new_session_id()
    second = new_session_id()

    assert first != second
    assert first.startswith("sess-")
    assert check_session_id(first) == first
    assert check_session_id(second) == second


def test_the_header_one_spelling_is_named_once_for_both_packages() -> None:
    """The OpenAI-compatible surface and any client read the same header name."""
    assert SESSION_HEADER == "x-potluck-session"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a" * (MAX_SESSION_ID_CHARS + 1),
        "two words",
        "lead ing",
        "trail ing ",
        "line\nbreak",
        "carriage\rreturn",
        "tab\there",
        "bell\x07char",
        "delete\x7fchar",
    ],
)
def test_a_session_identifier_that_cannot_travel_or_print_is_refused(value: str) -> None:
    """An empty, over-long, whitespace-bearing or control-bearing id is refused.

    Refused rather than trimmed or dropped: a caller that asked for a session and
    was silently given none would read the center's aggregate for a run it never
    joined.
    """
    with pytest.raises(ProtocolError):
        check_session_id(value)


def test_a_session_at_the_documented_length_is_accepted() -> None:
    """The bound is inclusive, so the longest legal identifier still works."""
    value = "s" * MAX_SESSION_ID_CHARS

    assert check_session_id(value) == value
