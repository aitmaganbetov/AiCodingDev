"""Репозиторий бэкапов: раскладка каталогов, подписанные манифесты, файл статуса для метрик/алертов.

Раскладка (все файлы данных — `*.enc`, AES-256-GCM, см. `crypto`):

    <repo>/REPO.json                         формат и key_id (без ключа)
    <repo>/base/<id>/manifest.json           физический бэкап PostgreSQL (pg_basebackup)
    <repo>/base/<id>/base.tar.gz.enc
    <repo>/wal/<сегмент|*.history>.enc       непрерывный архив WAL (PITR)
    <repo>/dump/<id>/manifest.json           логический дамп (pg_dump -Fc)
    <repo>/dump/<id>/aicd.dump.enc
    <repo>/artifacts/objects/<aa>/<sha256>.enc   объекты артефактов (content-addressed, общие для бэкапов)
    <repo>/artifacts/<id>/manifest.json      снимок: ключ объекта → sha256, проверка по artifacts.sha256
    <repo>/secrets/<id>/manifest.json        секреты/конфигурация (tar)
    <repo>/secrets/<id>/secrets.tar.enc
    <repo>/status.json                       последний результат по компонентам (метрика aicd_backup_*)

Манифест подписан HMAC-SHA256 подключом ключа бэкапа: подмена манифеста (например, «нужных» sha256)
без ключа невозможна.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.backup.crypto import BackupCryptoError, BackupKey

REPO_FORMAT = "aicd-backup-repo/1"
MANIFEST_FORMAT = "aicd-backup-manifest/1"
COMPONENTS = ("postgres_base", "postgres_wal", "postgres_dump", "artifacts", "secrets")
_ID_RE = re.compile(r"^\d{8}T\d{6}Z(-[0-9a-f]{4})?$")


class RepositoryError(Exception):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


def new_backup_id(now: datetime | None = None) -> str:
    return (now or utc_now()).strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(2).hex()


def _write_json_atomic(path: Path, data: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".partial")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass(frozen=True)
class Repository:
    root: Path
    key: BackupKey

    # --- инициализация ------------------------------------------------------------------------------

    @classmethod
    def open(cls, root: Path, key: BackupKey, *, create: bool = False) -> Repository:
        meta = root / "REPO.json"
        if not meta.exists():
            if not create:
                raise RepositoryError(f"{root}: не репозиторий бэкапов (нет REPO.json; создайте командой init)")
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            _write_json_atomic(meta, {"format": REPO_FORMAT, "key_id": key.key_id_hex, "created_at": iso(utc_now())})
        data = json.loads(meta.read_text(encoding="utf-8"))
        if data.get("format") != REPO_FORMAT:
            raise RepositoryError(f"{root}: неизвестный формат {data.get('format')!r}")
        if data.get("key_id") != key.key_id_hex:
            raise RepositoryError(f"{root}: репозиторий создан с другим ключом (key_id {data.get('key_id')})")
        return cls(root=root, key=key)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Эксклюзивная блокировка репозитория (параллельные бэкап/prune не смешиваются)."""
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RepositoryError("репозиторий занят другой командой бэкапа") from exc
            yield
        finally:
            os.close(fd)

    # --- манифесты ---------------------------------------------------------------------------------

    def set_dir(self, kind: str, backup_id: str) -> Path:
        if not _ID_RE.fullmatch(backup_id):
            raise RepositoryError(f"некорректный id бэкапа {backup_id!r}")
        return self.root / kind / backup_id

    def write_manifest(self, kind: str, backup_id: str, body: dict[str, Any]) -> Path:
        payload = {"format": MANIFEST_FORMAT, "kind": kind, "backup_id": backup_id, "key_id": self.key.key_id_hex}
        payload |= body
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        path = self.set_dir(kind, backup_id) / "manifest.json"
        _write_json_atomic(path, {"manifest": payload, "hmac_sha256": self.key.sign(canonical)})
        return path

    def read_manifest(self, kind: str, backup_id: str) -> dict[str, Any]:
        path = self.set_dir(kind, backup_id) / "manifest.json"
        if not path.is_file():
            raise RepositoryError(f"нет манифеста {kind}/{backup_id}")
        doc = json.loads(path.read_text(encoding="utf-8"))
        payload = doc.get("manifest")
        if not isinstance(payload, dict):
            raise RepositoryError(f"{path}: повреждён")
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        if not self.key.verify(canonical, str(doc.get("hmac_sha256", ""))):
            raise BackupCryptoError(f"{path}: подпись манифеста не совпадает (подмена или другой ключ)")
        if payload.get("kind") != kind or payload.get("backup_id") != backup_id:
            raise BackupCryptoError(f"{path}: манифест относится к другому бэкапу")
        return payload

    def list_ids(self, kind: str) -> list[str]:
        base = self.root / kind
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir() if _ID_RE.fullmatch(p.name) and (p / "manifest.json").is_file())

    def resolve_id(self, kind: str, backup_id: str) -> str:
        if backup_id != "latest":
            return backup_id
        ids = self.list_ids(kind)
        if not ids:
            raise RepositoryError(f"в репозитории нет бэкапов вида {kind}")
        return ids[-1]

    # --- статус (метрики aicd_backup_*, алерты AicdBackup*) ------------------------------------------

    def record_status(
        self, component: str, *, ok: bool, error: str | None = None, extra_status_file: Path | None = None
    ) -> None:
        if component not in COMPONENTS:
            raise ValueError(component)
        for path in filter(None, (self.root / "status.json", extra_status_file)):
            record_status_file(path, component, ok=ok, error=error)


def record_status_file(path: Path, component: str, *, ok: bool, error: str | None = None) -> None:
    """Обновляет файл статуса: время последней попытки/успеха и исход по компоненту (без секретов)."""
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    components = data.setdefault("components", {})
    now = time.time()
    entry = components.setdefault(component, {})
    entry["last_attempt_unixtime"] = now
    entry["last_status"] = "success" if ok else "failure"
    if ok:
        entry["last_success_unixtime"] = now
        entry.pop("last_error", None)
    else:
        entry["last_error"] = (error or "unknown error")[:300]
    data["format"] = "aicd-backup-status/1"
    _write_json_atomic(path, data, mode=0o644)  # без секретов: читает процесс метрик
