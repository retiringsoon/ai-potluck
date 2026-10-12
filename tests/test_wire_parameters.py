"""The decimal-parameter encoding is canonical, idempotent, and digest-stable.

A fraction cannot cross canonical JSON as a number: `canonicalize` refuses
floats, so the wire carries `DECIMAL_CHAT_PARAMETERS` values as decimal
strings. That only works if `encode_chat_parameters` accepts its own output —
a consumer signing `0.7`, the string `"0.7"` it becomes, and the center's
re-encode must all produce the same bytes, or the request digest falls apart
somewhere along the signed path.
"""

import pytest

from aipotluck.errors import ProtocolError
from aipotluck.wire import compute_request_digest, encode_chat_parameters


def test_fraction_encodes_to_a_decimal_string() -> None:
    assert encode_chat_parameters({"temperature": 0.7}) == {"temperature": "0.7"}


def test_encoding_is_idempotent_across_representations() -> None:
    for value in (0.7, "0.7", 1, 1.0, "1", "1.0", 0.001, "0.001", "1e-3"):
        once = encode_chat_parameters({"top_p": value})
        assert encode_chat_parameters(once) == once, value


def test_representations_of_one_value_agree() -> None:
    spellings = [0.7, "0.7", "7e-1", 7e-1]
    encoded = {encode_chat_parameters({"temperature": v})["temperature"] for v in spellings}
    assert encoded == {"0.7"}


def test_integral_values_encode_as_integers() -> None:
    for value in (1, 1.0, "1", "1.0"):
        assert encode_chat_parameters({"temperature": value}) == {"temperature": 1}, value


def test_digest_is_stable_across_representations() -> None:
    messages: tuple = ()
    as_number = compute_request_digest(
        messages=messages, model="m", delivery_mode="buffered_complete",
        parameters=encode_chat_parameters({"temperature": 0.7}),
    )
    as_string = compute_request_digest(
        messages=messages, model="m", delivery_mode="buffered_complete",
        parameters=encode_chat_parameters({"temperature": "0.7"}),
    )
    assert as_number == as_string


@pytest.mark.parametrize("bad", ["abc", "inf", "-inf", "nan", "", object(), True])
def test_invalid_values_fail_loud(bad: object) -> None:
    with pytest.raises(ProtocolError, match="temperature"):
        encode_chat_parameters({"temperature": bad})
