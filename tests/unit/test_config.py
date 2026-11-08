from pathlib import Path

import pytest

from aicd_platform.config import Settings


def test_env_parsing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "key"
    secret.write_text("s" * 40 + "\n")
    monkeypatch.setenv("AICD_ALLOWED_ORIGINS", "http://localhost:8080, https://aicd.example")
    monkeypatch.setenv("AICD_REPOSITORY_ROOTS", "main=/srv/repos,extra=/data/git")
    monkeypatch.setenv("AICD_SECRET_KEY_FILE", str(secret))
    s = Settings()
    assert s.allowed_origins == ["http://localhost:8080", "https://aicd.example"]
    assert s.repository_roots == {"main": Path("/srv/repos"), "extra": Path("/data/git")}
    assert s.require_secret_key() == b"s" * 40


@pytest.mark.parametrize("roots", ["main=relative/path", "bad name=/x"])
def test_invalid_roots(monkeypatch: pytest.MonkeyPatch, roots: str) -> None:
    monkeypatch.setenv("AICD_REPOSITORY_ROOTS", roots)
    with pytest.raises(ValueError, match=r"корн|корен"):
        Settings()


def test_short_secret_rejected() -> None:
    with pytest.raises(RuntimeError):
        Settings(secret_key="short").require_secret_key()
