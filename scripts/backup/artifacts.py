"""Артефакты: зеркало хранилища (S3/MinIO/RustFS или FS) с проверкой целостности по `artifacts.sha256`.

Порядок бэкапа: сначала строки `artifacts` из БД (роль aicd_maintenance, BYPASSRLS — все workspace),
затем объекты хранилища. Объект пишется в хранилище до строки, поэтому каждая строка, прочитанная до обхода,
обязана иметь объект: отсутствие объекта или несовпадение sha256 — ошибка целостности (бэкап FAILED,
алерт). Объекты без строки (сироты, архив retention) тоже копируются и попадают в отчёт.

Объекты хранятся content-addressed (`artifacts/objects/<aa>/<sha256>.enc`): неизменяемые артефакты не
копируются повторно, снимок — подписанный манифест «ключ → sha256».
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import psycopg

from scripts.backup.crypto import decrypt_file, encrypt_file
from scripts.backup.docker import PgTarget, psql
from scripts.backup.repo import Repository, iso, new_backup_id, utc_now

CHUNK = 1024 * 1024


class BackupIntegrityError(RuntimeError):
    def __init__(self, message: str, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


class ArtifactSource(Protocol):
    def ref_prefix(self) -> str: ...

    def keys(self) -> Iterator[str]: ...

    def read(self, key: str) -> Iterator[bytes]: ...

    def write(self, key: str, chunks: Iterator[bytes], sha256: str, size: int) -> None: ...

    def exists(self, key: str) -> bool: ...


@dataclass
class S3Source:
    endpoint: str
    bucket: str
    access_key: str
    secret_key: str
    region: str = "us-east-1"
    _client: Any = field(default=None, init=False, repr=False)

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name=self.region,
                config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
            )
        return self._client

    @classmethod
    def from_files(cls, endpoint: str, bucket: str, access_key_file: Path, secret_key_file: Path) -> S3Source:
        return cls(
            endpoint=endpoint,
            bucket=bucket,
            access_key=access_key_file.read_text(encoding="utf-8").strip(),
            secret_key=secret_key_file.read_text(encoding="utf-8").strip(),
        )

    def ref_prefix(self) -> str:
        return f"s3://{self.bucket}/"

    def ensure_bucket(self) -> None:
        from botocore.exceptions import ClientError

        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self.client.create_bucket(Bucket=self.bucket)

    def keys(self) -> Iterator[str]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket):
            for obj in page.get("Contents", []):
                yield str(obj["Key"])

    def read(self, key: str) -> Iterator[bytes]:
        body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
        while chunk := body.read(CHUNK):
            yield chunk

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError:
            return False
        return True

    def write(self, key: str, chunks: Iterator[bytes], sha256: str, size: int) -> None:
        body = b"".join(chunks)  # объекты артефактов ограничены лимитами платформы; ChecksumSHA256 проверит сервер
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentLength=size,
            ChecksumSHA256=base64.b64encode(bytes.fromhex(sha256)).decode(),
            Metadata={"sha256": sha256},
        )


@dataclass
class FsSource:
    root: Path

    def ref_prefix(self) -> str:
        return "file://artifacts/"

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"ключ выходит за пределы хранилища: {key!r}")
        return path

    def keys(self) -> Iterator[str]:
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not path.name.startswith(".aicd-upload-"):
                yield path.relative_to(self.root).as_posix()

    def read(self, key: str) -> Iterator[bytes]:
        with self._path(key).open("rb") as fh:
            while chunk := fh.read(CHUNK):
                yield chunk

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def write(self, key: str, chunks: Iterator[bytes], sha256: str, size: int) -> None:
        del sha256, size
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = target.with_name(f".aicd-upload-restore-{target.name}")
        with tmp.open("wb") as out:
            for chunk in chunks:
                out.write(chunk)
        tmp.chmod(0o400)
        tmp.rename(target)


ARTIFACT_ROWS_SQL = "SELECT storage_ref, sha256, size_bytes FROM aicd.artifacts"
RowsLoader = Callable[[], dict[str, tuple[str, int]]]


def db_artifacts(dsn: str) -> dict[str, tuple[str, int]]:
    """storage_ref → (sha256, size_bytes) всех workspace (роль aicd_maintenance: BYPASSRLS, только SELECT)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(ARTIFACT_ROWS_SQL).fetchall()
    return {str(ref): (str(sha), int(size)) for ref, sha, size in rows}


def db_artifacts_psql(target: PgTarget) -> dict[str, tuple[str, int]]:
    """То же через `psql` в контейнере (compose: порт PostgreSQL не публикуется на хост)."""
    out = psql(target, ARTIFACT_ROWS_SQL)
    result: dict[str, tuple[str, int]] = {}
    for line in filter(None, out.splitlines()):
        ref, sha, size = line.split("|")
        result[ref] = (sha, int(size))
    return result


def rows_loader(dsn: str | None = None, target: PgTarget | None = None) -> RowsLoader:
    if target is not None:
        return lambda: db_artifacts_psql(target)
    if dsn is None:
        raise ValueError("нужен DSN или PgTarget")
    return lambda: db_artifacts(dsn)


def _hash_chunks(chunks: Iterator[bytes], digest: Any, counter: list[int]) -> Iterator[bytes]:
    for chunk in chunks:
        digest.update(chunk)
        counter[0] += len(chunk)
        yield chunk


def object_path(repo: Repository, sha256: str) -> Path:
    return repo.root / "artifacts" / "objects" / sha256[:2] / f"{sha256}.enc"


def previous_objects(repo: Repository, prefix: str) -> dict[str, dict[str, Any]]:
    """Объекты последнего снимка того же хранилища (для инкрементального бэкапа неизменяемых артефактов)."""
    ids = repo.list_ids("artifacts")
    if not ids:
        return {}
    manifest = repo.read_manifest("artifacts", ids[-1])
    if manifest.get("source") != prefix:
        return {}
    return {k: v for k, v in manifest["objects"].items() if object_path(repo, v["sha256"]).exists()}


def backup_artifacts(repo: Repository, source: ArtifactSource, load_rows: RowsLoader, *, full: bool = False) -> dict[str, Any]:
    """Снимок хранилища. Инкрементально: ключи из прошлого снимка не перечитываются (артефакты неизменяемы:
    FS — `link()` без перезаписи, S3 — отказ при существующем ключе); `full=True` — перечитать и пересчитать sha256
    всех объектов (периодическая проверка целостности хранилища)."""
    started = utc_now()
    rows = load_rows()  # до обхода хранилища (см. docstring модуля)
    backup_id = new_backup_id()
    prefix = source.ref_prefix()
    previous = {} if full else previous_objects(repo, prefix)
    objects: dict[str, dict[str, Any]] = {}
    new_objects = reused = 0
    for key in source.keys():
        if key in previous:
            objects[key] = previous[key]
            reused += 1
            continue
        digest = hashlib.sha256()
        counter = [0]
        staging = repo.root / "artifacts" / "objects" / ".staging.enc"
        stats = encrypt_file(repo.key, "artifacts/object", _hash_chunks(source.read(key), digest, counter), staging)
        sha = digest.hexdigest()
        final = object_path(repo, sha)
        if final.exists():
            staging.unlink()
        else:
            # имя AAD фиксировано ("artifacts/object"): объект адресуется своим sha256, проверяемым после расшифровки
            final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            staging.rename(final)
            new_objects += 1
        objects[key] = {"sha256": sha, "size": counter[0], "ciphertext_sha256": stats.ciphertext_sha256}
    report = compare(rows, {k: (v["sha256"], v["size"]) for k, v in objects.items()}, prefix)
    manifest = {
        "started_at": iso(started),
        "finished_at": iso(utc_now()),
        "source": prefix,
        "objects": objects,
        "db_rows": len(rows),
        "new_objects": new_objects,
        "reused_from_previous": reused,
        "full": full,
        "integrity": report,
    }
    repo.write_manifest("artifacts", backup_id, manifest)
    summary = {"backup_id": backup_id, "objects": len(objects), "new_objects": new_objects, "reused": reused, **report}
    if report["missing"] or report["mismatched"]:
        raise BackupIntegrityError("артефакты не совпадают с artifacts.sha256", summary)
    return summary


def compare(rows: dict[str, tuple[str, int]], objects: dict[str, tuple[str, int]], prefix: str) -> dict[str, Any]:
    """Сверка строк `artifacts` (только этого хранилища) с объектами: ok / missing / mismatched / orphans."""
    ok = 0
    missing: list[str] = []
    mismatched: list[str] = []
    seen: set[str] = set()
    for ref, (sha, size) in sorted(rows.items()):
        if not ref.startswith(prefix):
            continue
        key = ref.removeprefix(prefix)
        seen.add(key)
        found = objects.get(key)
        if found is None:
            missing.append(key)
        elif not hmac.compare_digest(found[0], sha) or found[1] != size:
            mismatched.append(key)
        else:
            ok += 1
    orphans = sorted(set(objects) - seen)
    return {
        "rows_checked": len(seen),
        "ok": ok,
        "missing": missing,
        "mismatched": mismatched,
        "orphans": len(orphans),
    }


def restore_artifacts(repo: Repository, backup_id: str, sink: ArtifactSource) -> dict[str, Any]:
    """Объекты снимка → хранилище (существующие с тем же sha256 пропускаются); sha256 проверяется при расшифровке."""
    backup_id = repo.resolve_id("artifacts", backup_id)
    manifest = repo.read_manifest("artifacts", backup_id)
    restored = skipped = 0
    for key, meta in sorted(manifest["objects"].items()):
        if sink.exists(key):
            skipped += 1
            continue
        digest = hashlib.sha256()
        counter = [0]
        chunks = list(_hash_chunks(decrypt_file(repo.key, "artifacts/object", object_path(repo, meta["sha256"])),
                                   digest, counter))  # fmt: skip
        if digest.hexdigest() != meta["sha256"] or counter[0] != meta["size"]:
            raise BackupIntegrityError(f"объект {key}: содержимое бэкапа не совпадает с манифестом", {"key": key})
        sink.write(key, iter(chunks), meta["sha256"], meta["size"])
        restored += 1
    return {"backup_id": backup_id, "restored": restored, "skipped_existing": skipped}


def verify_storage(
    source: ArtifactSource, load_rows: RowsLoader, *, snapshot_keys: set[str] | None = None
) -> dict[str, Any]:
    """Каждая строка `artifacts` этого хранилища: объект есть и sha256/размер совпадают (полное чтение).

    `snapshot_keys` — ключи последнего восстановленного снимка: строка без объекта, чей ключ в снимок не попал,
    создана после снимка (PITR базы ушёл дальше снимка артефактов) — это потеря в пределах RPO артефактов
    (`missing_after_snapshot`), а не повреждение; отсутствующий объект из снимка — ошибка целостности (`missing`).
    """
    rows = load_rows()
    prefix = source.ref_prefix()
    objects: dict[str, tuple[str, int]] = {}
    for ref in rows:
        if not ref.startswith(prefix):
            continue
        key = ref.removeprefix(prefix)
        if not source.exists(key):
            continue
        digest = hashlib.sha256()
        counter = [0]
        for _ in _hash_chunks(source.read(key), digest, counter):
            pass
        objects[key] = (digest.hexdigest(), counter[0])
    report = compare(rows, objects, prefix)
    if snapshot_keys is not None:
        after = [key for key in report["missing"] if key not in snapshot_keys]
        report["missing"] = [key for key in report["missing"] if key in snapshot_keys]
        report["missing_after_snapshot"] = after
    return report


def snapshot_keys(repo: Repository, backup_id: str) -> set[str]:
    manifest = repo.read_manifest("artifacts", repo.resolve_id("artifacts", backup_id))
    return set(manifest["objects"])
