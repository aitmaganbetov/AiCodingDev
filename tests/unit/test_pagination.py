import pytest

from aicd_platform.errors import ProblemError
from aicd_platform.pagination import decode_cursor, encode_cursor

KEY = b"k" * 32


def test_roundtrip() -> None:
    c = encode_cursor(KEY, ["2026-01-01T00:00:00+00:00", "id"], {"ws": "a"})
    assert decode_cursor(KEY, c, {"ws": "a"}) == ["2026-01-01T00:00:00+00:00", "id"]


@pytest.mark.parametrize("mutate", ["filters", "tamper", "garbage", "other_key"])
def test_invalid_cursor_rejected(mutate: str) -> None:
    c = encode_cursor(KEY, [1], {"ws": "a"})
    with pytest.raises(ProblemError) as exc:
        if mutate == "filters":
            decode_cursor(KEY, c, {"ws": "b"})
        elif mutate == "tamper":
            body, sig = c.split(".")
            decode_cursor(KEY, body[:-2] + "AA." + sig, {"ws": "a"})
        elif mutate == "garbage":
            decode_cursor(KEY, "not-a-cursor", {"ws": "a"})
        else:
            decode_cursor(b"x" * 32, c, {"ws": "a"})
    assert exc.value.code == "invalid_cursor"
