"""Хранилище артефактов: файловая система и S3-совместимое (MinIO) — P1-15, OQ-11.

Ключ объекта = `<workspace_id>/<yyyy>/<mm>/<artifact_id>` (DATABASE_DESIGN §12.1): префикс
workspace изолирует объекты на уровне хранилища, `storage_ref` уникален для строки `artifacts`.
Содержимое неизменяемо; sha256 вычисляется при записи и проверяется при чтении
(ТЗ §15 «artifact integrity checks», AC-14). Ключ строится только из UUID и даты,
поэтому path traversal невозможен по построению; входящие ссылки дополнительно валидируются.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from aicd_platform.clock import utcnow
from aicd_platform.config import Settings

CHUNK = 1024 * 1024


class ArtifactIntegrityError(Exception):
    """Содержимое хранилища не совпадает с ожидаемым sha256."""


class ArtifactNotFoundError(Exception):
    pass


@dataclass(frozen=True)
class StoredArtifact:
    storage_ref: str
    sha256: str
    size_bytes: int


_KEY_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/\d{4}/\d{2}/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def object_key(workspace_id: uuid.UUID, artifact_id: uuid.UUID, created: datetime) -> str:
    return f"{workspace_id}/{created:%Y}/{created:%m}/{artifact_id}"


# Ключ объекта артефакта: `<workspace_id>/<YYYY>/<MM>/<artifact_id>` (object_key). Только такие объекты — кандидаты
# сборки сирот: архивы retention (`archive/…`) и прочие префиксы хранилища не трогаются.
_OBJECT_KEY_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/\d{4}/\d{2}/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


@dataclass(frozen=True)
class StoredObject:
    """Объект хранилища артефактов (для сборки сирот, P9-03)."""

    storage_ref: str
    modified_at: datetime
    size_bytes: int


def _validate_key(key: str) -> str:
    if not _KEY_RE.fullmatch(key):
        raise ValueError("некорректный ключ артефакта")
    return key


class ArtifactStore(Protocol):
    def put(self, workspace_id: uuid.UUID, artifact_id: uuid.UUID, stream: BinaryIO) -> StoredArtifact: ...

    def open(self, storage_ref: str, *, expected_sha256: str | None = None) -> Iterator[bytes]: ...

    def exists(self, storage_ref: str) -> bool: ...

    def healthy(self) -> bool: ...


def _spool(stream: BinaryIO, directory: str | None = None) -> tuple[str, str, int]:
    """Копирует поток во временный файл, считая sha256 и размер."""
    digest = hashlib.sha256()
    size = 0
    fd, tmp_path = tempfile.mkstemp(prefix=".aicd-upload-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := stream.read(CHUNK):
                digest.update(chunk)
                size += len(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        os.unlink(tmp_path)
        raise
    return tmp_path, digest.hexdigest(), size


def _verified_chunks(chunks: Iterator[bytes], expected_sha256: str | None) -> Iterator[bytes]:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
        yield chunk
    if expected_sha256 is not None and not hmac.compare_digest(digest.hexdigest(), expected_sha256):
        raise ArtifactIntegrityError("sha256 содержимого не совпадает с ожидаемым")


class FileSystemArtifactStore:
    """Локальная ФС; `storage_ref` = `file://artifacts/<key>`."""

    prefix = "file://artifacts/"

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):
            raise ValueError("ключ выходит за пределы хранилища")
        return path

    def _key(self, storage_ref: str) -> str:
        if not storage_ref.startswith(self.prefix):
            raise ValueError("ожидалась ссылка file://artifacts/…")
        return _validate_key(storage_ref.removeprefix(self.prefix))

    def put(self, workspace_id: uuid.UUID, artifact_id: uuid.UUID, stream: BinaryIO) -> StoredArtifact:
        key = object_key(workspace_id, artifact_id, utcnow())
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp_path, sha, size = _spool(stream, directory=str(target.parent))
        try:
            os.chmod(tmp_path, 0o400)
            # link() не перезаписывает существующий объект: артефакты неизменяемы.
            os.link(tmp_path, target)
        finally:
            os.unlink(tmp_path)
        return StoredArtifact(storage_ref=self.prefix + key, sha256=sha, size_bytes=size)

    def exists(self, storage_ref: str) -> bool:
        return self._path(self._key(storage_ref)).is_file()

    def delete(self, storage_ref: str) -> bool:
        """Удаление объекта (только retention, P3-18); False — объекта уже нет."""
        try:
            self._path(self._key(storage_ref)).unlink()
        except FileNotFoundError:
            return False
        return True

    def iter_objects(self) -> Iterator[StoredObject]:
        """Объекты артефактов (ключи формата `object_key`); временные файлы и архивы не перечисляются."""
        for path in self._root.glob("*/*/*/*"):
            key = path.relative_to(self._root).as_posix()
            if not _OBJECT_KEY_RE.fullmatch(key):
                continue
            try:
                st = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(st.st_mode):
                yield StoredObject(self.prefix + key, datetime.fromtimestamp(st.st_mtime, UTC), st.st_size)

    def healthy(self) -> bool:
        return self._root.is_dir() and os.access(self._root, os.W_OK)

    def open(self, storage_ref: str, *, expected_sha256: str | None = None) -> Iterator[bytes]:
        path = self._path(self._key(storage_ref))
        if not path.is_file():
            raise ArtifactNotFoundError(storage_ref)

        def chunks() -> Iterator[bytes]:
            with path.open("rb") as fh:
                while chunk := fh.read(CHUNK):
                    yield chunk

        return _verified_chunks(chunks(), expected_sha256)


def make_s3_client(*, endpoint_url: str, access_key: str, secret_key: str, region: str = "us-east-1") -> Any:
    """Клиент S3-совместимого хранилища (MinIO): SigV4, path-style адресация."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def ensure_s3_bucket(client: Any, bucket: str) -> None:
    from botocore.exceptions import ClientError

    try:
        client.head_bucket(Bucket=bucket)
    except ClientError:
        client.create_bucket(Bucket=bucket)


def s3_credentials(settings: Settings) -> tuple[str, str, str]:
    """endpoint, access key, secret key для S3 из настроек (ключи — из файлов секретов)."""
    if not (
        settings.artifact_s3_endpoint and settings.artifact_s3_access_key_file and settings.artifact_s3_secret_key_file
    ):
        raise RuntimeError("для S3 нужны AICD_ARTIFACT_S3_ENDPOINT и файлы ключей доступа")
    return (
        settings.artifact_s3_endpoint,
        Path(settings.artifact_s3_access_key_file).read_text().strip(),
        Path(settings.artifact_s3_secret_key_file).read_text().strip(),
    )


class S3ArtifactStore:
    """S3-совместимое хранилище (MinIO). Учётные данные — из файлов секретов."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
    ) -> None:
        self._bucket = bucket
        self._client = make_s3_client(
            endpoint_url=endpoint_url, access_key=access_key, secret_key=secret_key, region=region
        )

    def ensure_bucket(self) -> None:
        ensure_s3_bucket(self._client, self._bucket)

    @property
    def _prefix(self) -> str:
        return f"s3://{self._bucket}/"

    def _key(self, storage_ref: str) -> str:
        if not storage_ref.startswith(self._prefix):
            raise ValueError("ссылка относится к другому bucket")
        return _validate_key(storage_ref.removeprefix(self._prefix))

    def put(self, workspace_id: uuid.UUID, artifact_id: uuid.UUID, stream: BinaryIO) -> StoredArtifact:
        key = object_key(workspace_id, artifact_id, utcnow())
        if self._exists_key(key):
            raise FileExistsError(key)
        tmp_path, sha, size = _spool(stream)
        try:
            with open(tmp_path, "rb") as fh:
                # ChecksumSHA256: сервер сам проверяет целостность загруженного объекта.
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=fh,
                    ContentLength=size,
                    ChecksumSHA256=b64_sha256(sha),
                    Metadata={"sha256": sha},
                )
        finally:
            os.unlink(tmp_path)
        return StoredArtifact(storage_ref=self._prefix + key, sha256=sha, size_bytes=size)

    def _exists_key(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    def exists(self, storage_ref: str) -> bool:
        return self._exists_key(self._key(storage_ref))

    def delete(self, storage_ref: str) -> bool:
        """Удаление объекта (только retention, P3-18); False — объекта уже нет."""
        key = self._key(storage_ref)
        if not self._exists_key(key):
            return False
        self._client.delete_object(Bucket=self._bucket, Key=key)
        return True

    def iter_objects(self) -> Iterator[StoredObject]:
        """Объекты артефактов bucket (ключи формата `object_key`), постранично."""
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket):
            for item in page.get("Contents", []):
                key = str(item["Key"])
                if _OBJECT_KEY_RE.fullmatch(key):
                    modified = item["LastModified"]
                    yield StoredObject(self._prefix + key, modified.astimezone(UTC), int(item.get("Size", 0)))

    def healthy(self) -> bool:
        self._client.head_bucket(Bucket=self._bucket)
        return True

    def open(self, storage_ref: str, *, expected_sha256: str | None = None) -> Iterator[bytes]:
        from botocore.exceptions import ClientError

        key = self._key(storage_ref)
        try:
            obj = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            raise ArtifactNotFoundError(storage_ref) from exc
        body = obj["Body"]
        return _verified_chunks(iter(lambda: body.read(CHUNK), b""), expected_sha256)

    def presigned_get_url(self, storage_ref: str, *, ttl_seconds: int, filename: str | None = None) -> str:
        params: dict[str, str] = {"Bucket": self._bucket, "Key": self._key(storage_ref)}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{_safe_filename(filename)}"'
        url: str = self._client.generate_presigned_url("get_object", Params=params, ExpiresIn=ttl_seconds)
        return url


def b64_sha256(hex_sha: str) -> str:
    import base64

    return base64.b64encode(bytes.fromhex(hex_sha)).decode()


def _safe_filename(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in "._-")[:128] or "artifact"


# --- Подписанные ссылки для FS-хранилища (аналог presigned URL) -----------------------


def sign_download_token(key: bytes, artifact_id: uuid.UUID, user_id: uuid.UUID, ttl_seconds: int) -> str:
    expires = int(time.time()) + ttl_seconds
    msg = f"{artifact_id}:{user_id}:{expires}".encode()
    sig = hmac.new(key, msg, hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def verify_download_token(key: bytes, token: str, artifact_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    try:
        expires_s, sig = token.split(".", 1)
        expires = int(expires_s)
    except ValueError:
        return False
    if expires < int(time.time()):
        return False
    msg = f"{artifact_id}:{user_id}:{expires}".encode()
    return hmac.compare_digest(sig, hmac.new(key, msg, hashlib.sha256).hexdigest())


def build_artifact_store(settings: Settings) -> ArtifactStore:
    if settings.artifact_backend == "s3":
        endpoint, access_key, secret_key = s3_credentials(settings)
        return S3ArtifactStore(
            endpoint_url=endpoint,
            bucket=settings.artifact_s3_bucket,
            access_key=access_key,
            secret_key=secret_key,
            region=settings.artifact_s3_region,
        )
    return FileSystemArtifactStore(settings.artifact_fs_root)
