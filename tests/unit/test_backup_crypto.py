"""P9-05: шифрование файлов бэкапа (AES-256-GCM, STREAM) и подписанные манифесты репозитория."""

from __future__ import annotations

import io
import json
import secrets
from pathlib import Path

import pytest
from scripts.backup import crypto
from scripts.backup.artifacts import compare
from scripts.backup.crypto import BackupCryptoError, BackupKey
from scripts.backup.pg import WalArchiveConfig, _segment_gaps, wal_segment_of
from scripts.backup.repo import Repository, RepositoryError, record_status_file


@pytest.fixture
def key() -> BackupKey:
    return BackupKey.from_master(secrets.token_bytes(32))


def _encrypt(key: BackupKey, data: bytes, name: str = "f") -> bytes:
    out = io.BytesIO()
    pieces = [data[i : i + 70_000] for i in range(0, len(data), 70_000)] or [b""]
    stats = crypto.encrypt_stream(key, name, iter(pieces), out)
    assert stats.plaintext_bytes == len(data)
    return out.getvalue()


def _decrypt(key: BackupKey, blob: bytes, name: str = "f") -> bytes:
    return b"".join(crypto.decrypt_stream(key, name, io.BytesIO(blob)))


@pytest.mark.parametrize(
    "size", [0, 1, crypto.CHUNK_SIZE - 1, crypto.CHUNK_SIZE, crypto.CHUNK_SIZE + 1, 3 * crypto.CHUNK_SIZE]
)
def test_roundtrip_chunk_boundaries(key: BackupKey, size: int) -> None:
    data = secrets.token_bytes(size)
    blob = _encrypt(key, data)
    assert size < 16 or data not in blob  # короткая строка случайно встречается в шифротексте (1 байт — ~16%)
    assert _decrypt(key, blob) == data


def test_tamper_truncate_append_and_wrong_key_are_detected(key: BackupKey) -> None:
    data = secrets.token_bytes(2 * crypto.CHUNK_SIZE + 10)
    blob = _encrypt(key, data)
    flipped = bytearray(blob)
    flipped[crypto.HEADER_LEN + 100] ^= 0x01
    with pytest.raises(BackupCryptoError, match="целостность"):
        _decrypt(key, bytes(flipped))
    record = 4 + crypto.CHUNK_SIZE + crypto.TAG_LEN
    with pytest.raises(BackupCryptoError, match="усечён"):  # отброшена последняя запись
        _decrypt(key, blob[: crypto.HEADER_LEN + 2 * record])
    with pytest.raises(BackupCryptoError, match="после последней"):
        _decrypt(key, blob + b"x")
    with pytest.raises(BackupCryptoError, match="другим ключом"):
        _decrypt(BackupKey.from_master(secrets.token_bytes(32)), blob)
    with pytest.raises(BackupCryptoError, match="целостность"):  # файл подменён другим (AAD = имя)
        _decrypt(key, blob, name="other")
    # перестановка записей
    head, rest = blob[: crypto.HEADER_LEN], blob[crypto.HEADER_LEN :]
    swapped = head + rest[record : 2 * record] + rest[:record] + rest[2 * record :]
    with pytest.raises(BackupCryptoError):
        _decrypt(key, swapped)


def test_key_file_permissions_and_no_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "backup.key"
    generated = crypto.generate_key_file(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert BackupKey.load(path).key_id == generated.key_id
    with pytest.raises(FileExistsError):
        crypto.generate_key_file(path)
    path.chmod(0o644)
    with pytest.raises(BackupCryptoError, match="chmod 600"):
        BackupKey.load(path)


def test_manifest_signature_and_repo_key_binding(tmp_path: Path, key: BackupKey) -> None:
    repo = Repository.open(tmp_path / "repo", key, create=True)
    repo.write_manifest("base", "20261108T120000Z-abcd", {"files": [], "start_lsn": "0/1"})
    assert repo.read_manifest("base", "20261108T120000Z-abcd")["start_lsn"] == "0/1"
    assert repo.resolve_id("base", "latest") == "20261108T120000Z-abcd"
    path = tmp_path / "repo" / "base" / "20261108T120000Z-abcd" / "manifest.json"
    doc = json.loads(path.read_text())
    doc["manifest"]["start_lsn"] = "0/2"
    path.write_text(json.dumps(doc))
    with pytest.raises(BackupCryptoError, match="подпись"):
        repo.read_manifest("base", "20261108T120000Z-abcd")
    with pytest.raises(RepositoryError, match="другим ключом"):
        Repository.open(tmp_path / "repo", BackupKey.from_master(secrets.token_bytes(32)))
    with pytest.raises(RepositoryError, match="id"):
        repo.set_dir("base", "../etc")


def test_status_file_records_failure_then_success(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    record_status_file(path, "postgres_base", ok=False, error="boom")
    data = json.loads(path.read_text())["components"]["postgres_base"]
    assert data["last_status"] == "failure" and "last_success_unixtime" not in data
    record_status_file(path, "postgres_base", ok=True)
    data = json.loads(path.read_text())["components"]["postgres_base"]
    assert data["last_status"] == "success" and data["last_success_unixtime"] >= data["last_attempt_unixtime"] - 1


def test_wal_helpers() -> None:
    assert wal_segment_of("0/2000028", 1) == "000000010000000000000002"
    assert wal_segment_of("1/FF000000", 2) == "0000000200000001000000FF"
    assert _segment_gaps(["000000010000000000000001", "000000010000000000000002"]) == []
    assert _segment_gaps(["000000010000000000000001", "000000010000000000000003"])
    args = WalArchiveConfig(archive_timeout_seconds=30).server_args()
    assert "archive_mode=on" in args and "archive_timeout=30" in args
    assert any("mv " in a for a in args)  # атомарная публикация сегмента в архиве


def test_artifact_compare_reports_missing_mismatch_and_orphans() -> None:
    prefix = "s3://b/"
    rows = {prefix + "a": ("1" * 64, 1), prefix + "b": ("2" * 64, 2), "file://artifacts/x": ("3" * 64, 3)}
    objects = {"a": ("1" * 64, 1), "b": ("f" * 64, 2), "orphan": ("4" * 64, 4)}
    report = compare(rows, objects, prefix)
    assert report == {"rows_checked": 2, "ok": 1, "missing": [], "mismatched": ["b"], "orphans": 1}
    assert compare(rows, {}, prefix)["missing"] == ["a", "b"]
