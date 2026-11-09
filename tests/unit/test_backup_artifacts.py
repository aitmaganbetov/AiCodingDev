"""P9-05: зеркало артефактов (FS-хранилище) — снимок, инкремент, восстановление, сверка с artifacts.sha256;
метрики статуса бэкапа; system identifier сегмента WAL."""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path

import pytest
from scripts.backup import artifacts as art
from scripts.backup.crypto import BackupKey
from scripts.backup.pg import BackupError, segment_sysid
from scripts.backup.repo import Repository

from aicd_platform import metrics

WS = "0190a6f2-6c1e-7d2a-9b11-3f0e5c2d5a01"


def _key(i: int) -> str:
    return f"{WS}/2026/11/0190a6f2-6c1e-7d2a-9b11-3f0e5c2d5a{i:02d}"


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Repository, art.FsSource, dict[str, tuple[str, int]]]:
    repo = Repository.open(tmp_path / "repo", BackupKey.from_master(secrets.token_bytes(32)), create=True)
    store = art.FsSource(tmp_path / "artifacts")
    rows: dict[str, tuple[str, int]] = {}
    for i in range(3):
        data = secrets.token_bytes(1000 + i)
        path = store.root / _key(i)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        rows["file://artifacts/" + _key(i)] = (hashlib.sha256(data).hexdigest(), len(data))
    (store.root / "archive").mkdir()
    (store.root / "archive" / "run-events.ndjson.gz").write_bytes(b"retention archive")  # сирота: тоже копируется
    return repo, store, rows


def test_snapshot_incremental_restore_and_verify(
    setup: tuple[Repository, art.FsSource, dict[str, tuple[str, int]]], tmp_path: Path
) -> None:
    repo, store, rows = setup
    first = art.backup_artifacts(repo, store, lambda: rows)
    assert first["objects"] == 4 and first["new_objects"] == 4 and first["ok"] == 3 and first["orphans"] == 1
    second = art.backup_artifacts(repo, store, lambda: rows)
    assert second["reused"] == 4 and second["new_objects"] == 0  # неизменяемые объекты не перечитываются
    full = art.backup_artifacts(repo, store, lambda: rows, full=True)
    assert full["reused"] == 0 and full["new_objects"] == 0  # пересчитаны, но content-addressed — без дублей

    target = art.FsSource(tmp_path / "restored")
    restored = art.restore_artifacts(repo, "latest", target)
    assert restored["restored"] == 4
    assert art.verify_storage(target, lambda: rows)["ok"] == 3
    # строка, созданная после снимка (PITR базы ушёл дальше), — потеря в пределах RPO, а не повреждение
    late = dict(rows)
    late["file://artifacts/" + _key(9)] = ("0" * 64, 1)
    report = art.verify_storage(target, lambda: late, snapshot_keys=art.snapshot_keys(repo, "latest"))
    assert report["missing"] == [] and report["missing_after_snapshot"] == [_key(9)]


def test_integrity_errors_fail_backup(setup: tuple[Repository, art.FsSource, dict[str, tuple[str, int]]]) -> None:
    repo, store, rows = setup
    (store.root / _key(1)).write_bytes(b"tampered")
    with pytest.raises(art.BackupIntegrityError) as err:
        art.backup_artifacts(repo, store, lambda: rows)
    assert err.value.report["mismatched"] == [_key(1)]
    (store.root / _key(2)).unlink()
    with pytest.raises(art.BackupIntegrityError) as err:
        art.backup_artifacts(repo, store, lambda: rows, full=True)
    assert err.value.report["missing"] == [_key(2)]


def test_fs_source_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="пределы"):
        art.FsSource(tmp_path).exists("../etc/passwd")


def test_segment_sysid() -> None:
    header = bytearray(64)
    header[24:32] = (7705861321680220194).to_bytes(8, "little")
    assert segment_sysid(bytes(header)) == "7705861321680220194"
    with pytest.raises(BackupError):
        segment_sysid(b"short")


def test_backup_status_metrics() -> None:
    seen = metrics.observe_backup_status(
        {
            "components": {
                "postgres_base": {"last_status": "success", "last_success_unixtime": 1_700_000_000.0},
                "artifacts": {"last_status": "failure"},
                "../evil": {"last_status": "failure"},  # неизвестный компонент — не метка
            }
        }
    )
    assert seen == 2
    assert metrics.BACKUP_LAST_SUCCESS.labels("postgres_base")._value.get() == 1_700_000_000.0
    assert metrics.BACKUP_LAST_FAILED.labels("artifacts")._value.get() == 1
    assert metrics.BACKUP_LAST_FAILED.labels("postgres_base")._value.get() == 0
    assert metrics.observe_backup_status({"components": "broken"}) == 0


def test_worker_verification_images_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """OQ-43 (a): образы, собранные платформой, подключаются AICD_VERIFICATION_IMAGES поверх таблицы (по digest)."""
    import json

    from aicd_verification.images import ImagePolicyError
    from aicd_worker.settings import WorkerSettings
    from aicd_worker.wiring import default_ports

    ref = "aicd/migrations-python:17-3.12@sha256:" + "a" * 64
    monkeypatch.setenv("AICD_VERIFICATION_IMAGES", json.dumps({"migrations-python": ref}))
    settings = WorkerSettings(environment="production", database_url="postgresql://x@h/db")
    assert settings.verification_images == {"migrations-python": ref}
    _, verifier = default_ports(settings, engine=None, store=None)  # type: ignore[arg-type]
    assert verifier._images["migrations-python"] == ref  # type: ignore[attr-defined]
    monkeypatch.setenv("AICD_VERIFICATION_IMAGES", json.dumps({"php": "aicd/phpstan:latest"}))
    with pytest.raises(ImagePolicyError):
        default_ports(
            WorkerSettings(environment="production", database_url="postgresql://x@h/db"),
            engine=None,  # type: ignore[arg-type]
            store=None,  # type: ignore[arg-type]
        )
