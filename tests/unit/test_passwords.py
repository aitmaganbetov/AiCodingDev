import pytest

from aicd_identity import passwords, tokens


def test_argon2id_hash_and_verify() -> None:
    h = passwords.hash_password("Correct-Horse-Battery-9")
    assert h.startswith("$argon2id$")
    assert "m=65536,t=3,p=1" in h
    assert passwords.verify_password(h, "Correct-Horse-Battery-9")
    assert not passwords.verify_password(h, "wrong")
    assert not passwords.verify_password(None, "Correct-Horse-Battery-9")


@pytest.mark.parametrize("pw", ["short", "a" * 20, "x" * 1025, "aliceAlice-1234"])
def test_policy_rejects_weak(pw: str) -> None:
    with pytest.raises(passwords.WeakPasswordError):
        passwords.validate_password_policy(pw, email="alice@example.test")


def test_tokens_hash_only() -> None:
    t = tokens.new_token()
    assert len(t) >= 43
    assert tokens.matches(t, tokens.token_hash(t))
    assert not tokens.matches(t + "x", tokens.token_hash(t))
