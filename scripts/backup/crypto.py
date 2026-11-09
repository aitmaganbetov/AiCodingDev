"""Потоковое шифрование файлов бэкапа AES-256-GCM (SC-DATA-03) и подпись манифестов HMAC-SHA256.

Формат файла (`AICDBK01`):

    magic(8) | key_id(8) | nonce_prefix(7) | chunk_size(u32 BE) | записи…
    запись: len(u32 BE) | ciphertext+tag(16)

Nonce записи = nonce_prefix(7) ‖ номер записи (u32 BE) ‖ флаг последней записи (1 байт) — конструкция STREAM
(Hoang–Reyhanitabar–Rogaway–Vizár): перестановка, удаление, усечение и дописывание записей обнаруживаются.
AAD каждой записи = заголовок ‖ логическое имя файла: подмена одного файла бэкапа другим (например, сегмента
WAL) обнаруживается. Пустой поток = одна последняя запись нулевой длины.

Ключ — 32 случайных байта (base64 в файле `0600`), хранится **вне** репозитория бэкапов (офлайн-хранилище
оператора). Из него HKDF-SHA256 выводятся независимые подключи: шифрование данных, HMAC манифестов и
идентификатор ключа (не раскрывает ключ).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import stat
import struct
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"AICDBK01"
CHUNK_SIZE = 1024 * 1024
NONCE_PREFIX_LEN = 7
TAG_LEN = 16
HEADER_LEN = len(MAGIC) + 8 + NONCE_PREFIX_LEN + 4
MAX_RECORD = CHUNK_SIZE + TAG_LEN


class BackupCryptoError(Exception):
    """Файл бэкапа повреждён, подменён, усечён или зашифрован другим ключом."""


def _derive(master: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"aicd-backup-v1", info=info).derive(master)


@dataclass(frozen=True)
class BackupKey:
    data_key: bytes
    mac_key: bytes
    key_id: bytes  # 8 байт, не раскрывает ключ

    @property
    def key_id_hex(self) -> str:
        return self.key_id.hex()

    @classmethod
    def from_master(cls, master: bytes) -> BackupKey:
        if len(master) != 32:
            raise BackupCryptoError("ключ бэкапа должен содержать ровно 32 байта")
        return cls(
            data_key=_derive(master, b"data"),
            mac_key=_derive(master, b"manifest-mac"),
            key_id=_derive(master, b"key-id")[:8],
        )

    @classmethod
    def load(cls, path: Path) -> BackupKey:
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise BackupCryptoError(f"файл ключа {path} доступен группе/другим (нужно chmod 600)")
        try:
            master = base64.b64decode(path.read_text(encoding="ascii").strip(), validate=True)
        except ValueError as exc:
            raise BackupCryptoError("файл ключа должен содержать base64 32 байт") from exc
        return cls.from_master(master)

    def sign(self, payload: bytes) -> str:
        return hmac.new(self.mac_key, payload, hashlib.sha256).hexdigest()

    def verify(self, payload: bytes, signature: str) -> bool:
        return hmac.compare_digest(self.sign(payload), signature)


def generate_key_file(path: Path) -> BackupKey:
    """Новый ключ в файле `0600`; существующий файл не перезаписывается."""
    master = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(base64.b64encode(master).decode() + "\n")
    return BackupKey.from_master(master)


def _nonce(prefix: bytes, counter: int, final: bool) -> bytes:
    if counter >= 2**32:
        raise BackupCryptoError("слишком много записей в одном файле")
    return prefix + struct.pack(">I", counter) + (b"\x01" if final else b"\x00")


@dataclass
class EncryptStats:
    plaintext_bytes: int = 0
    plaintext_sha256: str = ""
    ciphertext_bytes: int = 0
    ciphertext_sha256: str = ""


def encrypt_stream(key: BackupKey, name: str, chunks: Iterable[bytes], out: BinaryIO) -> EncryptStats:
    """Шифрует поток `chunks` в `out`; возвращает размеры и sha256 открытого и шифрованного содержимого."""
    prefix = secrets.token_bytes(NONCE_PREFIX_LEN)
    header = MAGIC + key.key_id + prefix + struct.pack(">I", CHUNK_SIZE)
    aad = header + name.encode()
    aead = AESGCM(key.data_key)
    plain_hash = hashlib.sha256()
    cipher_hash = hashlib.sha256()
    stats = EncryptStats()

    def emit(data: bytes) -> None:
        out.write(data)
        cipher_hash.update(data)
        stats.ciphertext_bytes += len(data)

    emit(header)
    counter = 0
    buffer = bytearray()
    for chunk in chunks:
        plain_hash.update(chunk)
        stats.plaintext_bytes += len(chunk)
        buffer.extend(chunk)
        while len(buffer) > CHUNK_SIZE:  # строго больше: последняя (final) запись всегда остаётся в буфере
            sealed = aead.encrypt(_nonce(prefix, counter, False), bytes(buffer[:CHUNK_SIZE]), aad)
            emit(struct.pack(">I", len(sealed)) + sealed)
            counter += 1
            del buffer[:CHUNK_SIZE]
    sealed = aead.encrypt(_nonce(prefix, counter, True), bytes(buffer), aad)
    emit(struct.pack(">I", len(sealed)) + sealed)
    stats.plaintext_sha256 = plain_hash.hexdigest()
    stats.ciphertext_sha256 = cipher_hash.hexdigest()
    return stats


def _open_record(aead: AESGCM, prefix: bytes, counter: int, sealed: bytes, aad: bytes, name: str) -> tuple[bytes, bool]:
    """Запись проверяется с флагом «не последняя», затем «последняя»: подходит ровно одна интерпретация."""
    for final in (False, True):
        try:
            return aead.decrypt(_nonce(prefix, counter, final), sealed, aad), final
        except InvalidTag:
            continue
    raise BackupCryptoError(f"{name}: нарушена целостность (запись {counter})")


def _read_exact(src: IO[bytes], size: int) -> bytes:
    data = src.read(size)
    return data if data is not None else b""


def decrypt_stream(key: BackupKey, name: str, src: IO[bytes]) -> Iterator[bytes]:
    """Расшифровывает файл бэкапа; любое нарушение целостности — `BackupCryptoError` (до выдачи данных записи)."""
    header = _read_exact(src, HEADER_LEN)
    if len(header) != HEADER_LEN or not header.startswith(MAGIC):
        raise BackupCryptoError(f"{name}: не файл бэкапа AICDBK01")
    key_id = header[len(MAGIC) : len(MAGIC) + 8]
    if not hmac.compare_digest(key_id, key.key_id):
        raise BackupCryptoError(f"{name}: зашифрован другим ключом (key_id {key_id.hex()})")
    prefix = header[len(MAGIC) + 8 : len(MAGIC) + 8 + NONCE_PREFIX_LEN]
    aad = header + name.encode()
    aead = AESGCM(key.data_key)
    counter = 0
    while True:
        raw_len = _read_exact(src, 4)
        if len(raw_len) != 4:
            raise BackupCryptoError(f"{name}: файл усечён (нет последней записи)")
        (length,) = struct.unpack(">I", raw_len)
        if length < TAG_LEN or length > MAX_RECORD:
            raise BackupCryptoError(f"{name}: некорректная длина записи")
        sealed = _read_exact(src, length)
        if len(sealed) != length:
            raise BackupCryptoError(f"{name}: файл усечён")
        plain, final = _open_record(aead, prefix, counter, sealed, aad, name)
        counter += 1
        if final:
            if src.read(1):
                raise BackupCryptoError(f"{name}: данные после последней записи")
            if plain:
                yield plain
            return
        yield plain


def encrypt_file(key: BackupKey, name: str, chunks: Iterable[bytes], target: Path) -> EncryptStats:
    """Атомарная запись зашифрованного файла (`*.partial` → fsync → rename), права `0600`."""
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = target.with_name(target.name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            stats = encrypt_stream(key, name, chunks, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return stats


def decrypt_file(key: BackupKey, name: str, path: Path) -> Iterator[bytes]:
    with path.open("rb") as src:
        yield from decrypt_stream(key, name, src)


def read_chunks(src: IO[bytes], size: int = CHUNK_SIZE) -> Iterator[bytes]:
    while chunk := src.read(size):
        yield chunk
