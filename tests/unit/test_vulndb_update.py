"""OQ-43 (f): одобрение обновления офлайн-БД уязвимостей, проверка архива OSV, атомарная замена и аудит."""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from scripts.vulndb import update


def _approval(tmp_path: Path, **overrides: object) -> Path:
    data: dict[str, object] = {
        "approved_by": "security-officer@corp.example",
        "approved_at": datetime.now(UTC).isoformat(),
        "reason": "плановое обновление",
        "ticket": "OPS-1",
        "scope": ["osv"],
    }
    data |= overrides
    path = tmp_path / "approval.json"
    path.write_text(json.dumps(data))
    return path


def test_approval_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("getpass.getuser", lambda: "operator")
    assert update.load_approval(_approval(tmp_path), {"osv"})["ticket"] == "OPS-1"
    with pytest.raises(update.UpdateError, match="не покрывает"):
        update.load_approval(_approval(tmp_path), {"osv", "trivy"})
    stale = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
    with pytest.raises(update.UpdateError, match="просрочено"):
        update.load_approval(_approval(tmp_path, approved_at=stale), {"osv"})
    with pytest.raises(update.UpdateError, match="неполное"):
        update.load_approval(_approval(tmp_path, ticket=""), {"osv"})
    with pytest.raises(update.UpdateError, match="четыре глаза"):
        update.load_approval(_approval(tmp_path, approved_by="operator"), {"osv"})


def test_zip_validation(tmp_path: Path) -> None:
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("GHSA-xxxx-yyyy-zzzz.json", json.dumps({"id": "GHSA-xxxx-yyyy-zzzz"}))
    assert update._validate_zip(good) == 1
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escape.json", "{}")
    with pytest.raises(update.UpdateError, match="недопустимое имя"):
        update._validate_zip(evil)
    wrong = tmp_path / "wrong.zip"
    with zipfile.ZipFile(wrong, "w") as zf:
        zf.writestr("a.json", json.dumps(["not", "osv"]))
    with pytest.raises(update.UpdateError, match="формате OSV"):
        update._validate_zip(wrong)


def test_swap_keeps_previous_and_audit(tmp_path: Path) -> None:
    root = tmp_path / "db"
    for version in ("v1", "v2"):
        staging = root / f".staging-{version}"
        staging.mkdir(parents=True)
        (staging / "marker").write_text(version)
        update.swap(root, "osv", staging)
    assert (root / "osv" / "marker").read_text() == "v2"
    assert (root / ".previous" / "osv" / "marker").read_text() == "v1"
    update.audit(root, {"result": "success"})
    assert json.loads((root / "audit.jsonl").read_text().splitlines()[0]) == {"result": "success"}
