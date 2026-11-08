"""PostgreSQL: физический бэкап + архив WAL (PITR), логический дамп, восстановление и пост-шаги.

Проект бэкапа (SECURITY_ARCHITECTURE §14, SC-DATA-03): `pg_basebackup` + непрерывное архивирование WAL.
Сервер архивирует WAL сам (`archive_command`, атомарно `cp` → `mv` в том архива, `archive_timeout`
ограничивает RPO); `wal-sync` переносит сегменты из тома в зашифрованный репозиторий. Восстановление:
базовый бэкап → `pg_verifybackup` → recovery с `restore_command` из расшифрованного архива → promote.
"""

from __future__ import annotations

import io
import re
import subprocess
import tarfile
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from scripts.backup.crypto import decrypt_file, encrypt_file, read_chunks
from scripts.backup.docker import (
    POSTGRES_IMAGE,
    POSTGRES_UID,
    DockerError,
    PgTarget,
    docker,
    psql,
    streaming_tool,
    volume_args,
)
from scripts.backup.repo import Repository, RepositoryError, iso, new_backup_id, utc_now

WAL_NAME_RE = re.compile(r"^(?:[0-9A-F]{24}(?:\.[0-9A-F]{8}\.backup)?|[0-9A-F]{8}\.history)$")
SEGMENT_RE = re.compile(r"^[0-9A-F]{24}$")
LSN_RE = re.compile(r"write-ahead log (start|end) point: ([0-9A-F]+/[0-9A-F]+)(?: on timeline (\d+))?")
WAL_ARCHIVE_MOUNT = "/var/lib/postgresql/wal-archive"
WAL_RESTORE_MOUNT = "/var/lib/postgresql/wal-restore"
DATA_MOUNT = "/var/lib/postgresql/data"
# Сдвиг последовательности fencing token после PITR (DATABASE_DESIGN §18.6): токены, выданные после точки
# восстановления живым воркерам, не должны повториться.
FENCING_BUMP = 1_000_000
FENCING_SOURCES = (
    ("worker_leases", "fencing_token"),
    ("tasks", "lease_fencing_token"),
    ("task_attempts", "fencing_token"),
    ("cli_executions", "fencing_token"),
    ("external_actions", "fencing_token"),
)


class BackupError(RuntimeError):
    pass


def _drain(stream: IO[bytes] | None, sink: list[bytes]) -> threading.Thread:
    def run() -> None:
        if stream is not None:
            sink.append(stream.read())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def server_facts(target: PgTarget) -> dict[str, Any]:
    row = psql(
        target,
        "SELECT current_setting('server_version'), current_setting('archive_mode'),"
        " current_setting('archive_timeout'), current_setting('wal_level'),"
        " to_regclass('aicd.alembic_version') IS NOT NULL",
    ).split("|")
    revision = psql(target, "SELECT version_num FROM aicd.alembic_version LIMIT 1") if row[4] == "t" else ""
    return {
        "server_version": row[0],
        "archive_mode": row[1],
        "archive_timeout": row[2],
        "wal_level": row[3],
        "alembic_revision": revision or None,
    }


# --- физический бэкап ----------------------------------------------------------------------------------


def base_backup(repo: Repository, target: PgTarget, *, label: str = "aicd") -> dict[str, Any]:
    """`pg_basebackup` (tar+gzip в stdout, WAL fetch, manifest SHA256) → `base/<id>/base.tar.gz.enc`."""
    facts = server_facts(target)
    sysid = psql(target, "SELECT system_identifier FROM pg_control_system()")
    backup_id = new_backup_id()
    started = utc_now()
    out = repo.set_dir("base", backup_id) / "base.tar.gz.enc"
    argv = [
        "pg_basebackup",
        *target.conn_args(),
        "-D",
        "-",
        "-Ft",
        "-X",
        "fetch",
        "--compress=gzip:1",
        "--checkpoint=fast",
        "--manifest-checksums=SHA256",
        "-l",
        f"{label} {backup_id}",
        "-v",
        "--no-sync",
    ]
    stderr: list[bytes] = []
    with streaming_tool(target, argv) as proc:
        drain = _drain(proc.stderr, stderr)
        assert proc.stdout is not None
        stats = encrypt_file(repo.key, f"base/{backup_id}/base.tar.gz", read_chunks(proc.stdout), out)
        code = proc.wait()
        drain.join()
    log = b"".join(stderr).decode(errors="replace")
    if code != 0:
        out.unlink(missing_ok=True)
        raise BackupError(f"pg_basebackup завершился с кодом {code}: {log[-1500:]}")
    points = {m.group(1): (m.group(2), m.group(3)) for m in LSN_RE.finditer(log)}
    if "start" not in points or "end" not in points:
        out.unlink(missing_ok=True)
        raise BackupError(f"pg_basebackup не сообщил границы WAL: {log[-500:]}")
    finished = utc_now()
    manifest = {
        "started_at": iso(started),
        "finished_at": iso(finished),
        "start_lsn": points["start"][0],
        "end_lsn": points["end"][0],
        "timeline": int(points["start"][1] or 1),
        "start_wal_segment": wal_segment_of(points["start"][0], int(points["start"][1] or 1)),
        "system_identifier": sysid,
        "pg_image": target.image,
        "server": facts,
        "files": [{"name": "base.tar.gz", "path": out.name, **stats.__dict__}],
    }
    repo.write_manifest("base", backup_id, manifest)
    return {"backup_id": backup_id, **manifest}


def wal_segment_of(lsn: str, timeline: int, segment_size: int = 16 * 1024 * 1024) -> str:
    hi, lo = (int(part, 16) for part in lsn.split("/"))
    position = (hi << 32) | lo
    segno = position // segment_size
    per_xlogid = 0x100000000 // segment_size
    return f"{timeline:08X}{segno // per_xlogid:08X}{segno % per_xlogid:08X}"


def list_wal_archive(volume: str, image: str = POSTGRES_IMAGE) -> list[str]:
    out = docker(
        "run",
        "--rm",
        "--user",
        POSTGRES_UID,
        "--network",
        "none",
        *volume_args(volume, "/wal", read_only=True),
        image,
        "ls",
        "-1",
        "/wal",
    )
    return sorted(name for name in out.split() if WAL_NAME_RE.fullmatch(name))


def segment_sysid(header: bytes) -> str:
    """Идентификатор системы БД из длинного заголовка первой страницы сегмента WAL (XLogLongPageHeaderData.xlp_sysid,
    смещение 24, little-endian на x86-64/arm64)."""
    if len(header) < 32:
        raise BackupError("сегмент WAL короче заголовка страницы")
    return str(int.from_bytes(header[24:32], "little"))


def wal_dir(repo: Repository, sysid: str) -> Path:
    if not sysid.isdigit():
        raise RepositoryError(f"некорректный system identifier {sysid!r}")
    return repo.root / "wal" / sysid


def wal_names(repo: Repository, sysid: str) -> list[str]:
    return sorted(p.name.removesuffix(".enc") for p in wal_dir(repo, sysid).glob("*.enc"))


def _known_timelines(repo: Repository) -> dict[str, str]:
    """Таймлайн (8 hex) → sysid по сегментам, уже лежащим в репозитории."""
    known: dict[str, str] = {}
    for path in (repo.root / "wal").glob("*/*.enc"):
        name = path.name.removesuffix(".enc")
        if SEGMENT_RE.fullmatch(name):
            known.setdefault(name[:8], path.parent.name)
    return known


def wal_sync(
    repo: Repository, volume: str, *, image: str = POSTGRES_IMAGE, delete_synced: bool = False
) -> dict[str, Any]:
    """Новые файлы архива WAL из тома → `wal/<system_identifier>/<имя>.enc` (идемпотентно, атомарно по файлу).

    Пространство имён — идентификатор системы БД из заголовка сегмента: файлы другого кластера (том архива,
    переживший пересоздание кластера) не смешиваются с цепочкой WAL бэкапа. `.history`/`.backup` относятся к
    кластеру сегментов того же таймлайна; пока такого сегмента нет — файл остаётся в томе до следующего прогона.
    """
    available = list_wal_archive(volume, image)
    present = {p.name.removesuffix(".enc") for p in (repo.root / "wal").glob("*/*.enc")}
    pending = [name for name in available if name not in present]
    synced: list[str] = []
    deferred: list[str] = []
    timelines = _known_timelines(repo)
    sysids: set[str] = set()
    if pending:
        cmd = ["docker", "run", "--rm", "--user", POSTGRES_UID, "--network", "none"]
        cmd += [*volume_args(volume, "/wal", read_only=True), image, "tar", "-C", "/wal", "-cf", "-", *pending]
        stderr: list[bytes] = []
        small: dict[str, bytes] = {}
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # noqa: S603
        drain = _drain(proc.stderr, stderr)
        try:
            if proc.stdout is None:
                raise BackupError("нет stdout контейнера")
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                for member in tar:
                    if not member.isfile() or not WAL_NAME_RE.fullmatch(member.name):
                        continue
                    fh = tar.extractfile(member)
                    if fh is None:
                        continue
                    if not SEGMENT_RE.fullmatch(member.name):
                        small[member.name] = fh.read()  # .history / .backup — килобайты
                        continue
                    header = fh.read(8192)
                    sysid = segment_sysid(header)
                    sysids.add(sysid)
                    timelines.setdefault(member.name[:8], sysid)
                    chunks = iter([header, *read_chunks(fh)])
                    target = wal_dir(repo, sysid) / f"{member.name}.enc"
                    encrypt_file(repo.key, f"wal/{sysid}/{member.name}", chunks, target)
                    synced.append(member.name)
        finally:
            code = proc.wait()
            drain.join()
        if code != 0:
            raise BackupError(f"чтение архива WAL: {b''.join(stderr).decode(errors='replace')[-800:]}")
        for name, data in sorted(small.items()):
            owner = timelines.get(name[:8])
            if owner is None:
                deferred.append(name)
                continue
            encrypt_file(repo.key, f"wal/{owner}/{name}", iter([data]), wal_dir(repo, owner) / f"{name}.enc")
            synced.append(name)
    if delete_synced and synced:
        docker(
            "run",
            "--rm",
            "--user",
            POSTGRES_UID,
            "--network",
            "none",
            *volume_args(volume, "/wal"),
            image,
            "rm",
            "-f",
            *(f"/wal/{name}" for name in synced),
        )
    return {"available": len(available), "synced": synced, "deferred": deferred, "system_identifiers": sorted(sysids),
            "already_in_repo": len(available) - len(pending)}  # fmt: skip


# --- логический дамп -----------------------------------------------------------------------------------


def logical_dump(repo: Repository, target: PgTarget) -> dict[str, Any]:
    """`pg_dump -Fc` (BYPASSRLS-роль aicd_maintenance или суперпользователь) → `dump/<id>/aicd.dump.enc`."""
    facts = server_facts(target)
    backup_id = new_backup_id()
    started = utc_now()
    out = repo.set_dir("dump", backup_id) / "aicd.dump.enc"
    argv = ["pg_dump", *target.conn_args(), "-d", target.dbname, "-Fc"]
    stderr: list[bytes] = []
    with streaming_tool(target, argv) as proc:
        drain = _drain(proc.stderr, stderr)
        assert proc.stdout is not None
        stats = encrypt_file(repo.key, f"dump/{backup_id}/aicd.dump", read_chunks(proc.stdout), out)
        code = proc.wait()
        drain.join()
    if code != 0:
        out.unlink(missing_ok=True)
        log = b"".join(stderr).decode(errors="replace")
        errors = [line for line in log.splitlines() if "error" in line.lower()][:5]
        raise BackupError(f"pg_dump завершился с кодом {code}: {' | '.join(errors) or log[-1500:]}"[:2000])
    manifest = {
        "started_at": iso(started),
        "finished_at": iso(utc_now()),
        "role": target.user,
        "dbname": target.dbname,
        "pg_image": target.image,
        "server": facts,
        "files": [{"name": "aicd.dump", "path": out.name, **stats.__dict__}],
    }
    repo.write_manifest("dump", backup_id, manifest)
    return {"backup_id": backup_id, **manifest}


# --- проверка ------------------------------------------------------------------------------------------


def verify_set(repo: Repository, kind: str, backup_id: str) -> dict[str, Any]:
    """Полная расшифровка файлов набора: AEAD + sha256 открытого текста по подписанному манифесту."""
    import hashlib

    manifest = repo.read_manifest(kind, backup_id)
    checked = []
    for entry in manifest.get("files", []):
        path = repo.set_dir(kind, backup_id) / entry["path"]
        digest = hashlib.sha256()
        size = 0
        for chunk in decrypt_file(repo.key, f"{kind}/{backup_id}/{entry['name']}", path):
            digest.update(chunk)
            size += len(chunk)
        if digest.hexdigest() != entry["plaintext_sha256"] or size != entry["plaintext_bytes"]:
            raise BackupError(f"{kind}/{backup_id}/{entry['name']}: содержимое не совпадает с манифестом")
        checked.append(entry["name"])
    return {"kind": kind, "backup_id": backup_id, "files_verified": checked}


def verify_wal(repo: Repository, sysid: str, *, since_segment: str | None = None) -> dict[str, Any]:
    """Все файлы WAL кластера `sysid` (сегменты — с `since_segment`) расшифровываются; разрывы цепочки — ошибка."""
    names = wal_names(repo, sysid)
    segments = [n for n in names if SEGMENT_RE.fullmatch(n) and (since_segment is None or n[8:] >= since_segment[8:])]
    for name in names:
        if not SEGMENT_RE.fullmatch(name) or name in segments:
            for _ in decrypt_file(repo.key, f"wal/{sysid}/{name}", wal_dir(repo, sysid) / f"{name}.enc"):
                pass
    return {"system_identifier": sysid, "segments": len(segments), "first": segments[0] if segments else None,
            "last": segments[-1] if segments else None, "gaps": _segment_gaps(segments)}  # fmt: skip


def _segment_gaps(segments: list[str]) -> list[str]:
    gaps: list[str] = []
    by_tl: dict[str, list[int]] = {}
    for name in segments:
        by_tl.setdefault(name[:8], []).append(int(name[8:16], 16) * 0x100 + int(name[16:24], 16))
    for tl, numbers in by_tl.items():
        numbers.sort()
        for prev, cur in zip(numbers, numbers[1:], strict=False):
            if cur != prev + 1:
                gaps.append(f"timeline {tl}: после {prev:X} следует {cur:X}")
    return gaps


# --- восстановление ------------------------------------------------------------------------------------


def _volume_empty(volume: str, image: str) -> bool:
    out = docker(
        "run", "--rm", "--network", "none", *volume_args(volume, "/v", read_only=True), image, "ls", "-A", "/v"
    )
    return not out.strip()


def _pipe_into_container(cmd: list[str], chunks: Iterator[bytes]) -> None:
    stderr: list[bytes] = []
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)  # noqa: S603
    drain = _drain(proc.stderr, stderr)
    assert proc.stdin is not None
    try:
        for chunk in chunks:
            proc.stdin.write(chunk)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    except BaseException:
        proc.kill()
        raise
    finally:
        code = proc.wait()
        drain.join()
    if code != 0:
        raise BackupError(f"{' '.join(cmd[:3])}…: {b''.join(stderr).decode(errors='replace')[-1500:]}")


def restore_base(
    repo: Repository,
    backup_id: str,
    *,
    data_volume: str,
    wal_volume: str,
    target_time: datetime | None = None,
    image: str = POSTGRES_IMAGE,
) -> dict[str, Any]:
    """Базовый бэкап в пустой том данных + расшифрованный архив WAL в том восстановления + recovery-настройки.

    После этого сервер запускается с томами `data_volume` → PGDATA и `wal_volume` → /var/lib/postgresql/wal-restore;
    он воспроизводит WAL до конца архива (или `target_time`) и выполняет promote. Затем — `post_restore`.
    """
    backup_id = repo.resolve_id("base", backup_id)
    manifest = repo.read_manifest("base", backup_id)
    for volume in (data_volume, wal_volume):
        docker("volume", "create", volume)
        if not _volume_empty(volume, image):
            raise RepositoryError(f"том {volume} не пуст: восстановление только в пустой том")
    entry = manifest["files"][0]
    path = repo.set_dir("base", backup_id) / entry["path"]
    extract = (
        f"tar -xzf - -C {DATA_MOUNT} && chown -R {POSTGRES_UID} {DATA_MOUNT} && chmod 0700 {DATA_MOUNT}"
        f" && install -d -o 70 -g 70 -m 0700 {DATA_MOUNT}/pg_wal"
    )
    cmd = ["docker", "run", "-i", "--rm", "--network", "none", "--user", "0:0"]
    cmd += [*volume_args(data_volume, DATA_MOUNT), image, "sh", "-c", extract]
    _pipe_into_container(cmd, decrypt_file(repo.key, f"base/{backup_id}/{entry['name']}", path))
    verify = docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        POSTGRES_UID,
        *volume_args(data_volume, DATA_MOUNT),
        image,
        "pg_verifybackup",
        DATA_MOUNT,
        check=False,
    )
    wal = _stage_wal(repo, manifest["system_identifier"], manifest["start_wal_segment"], wal_volume, image)
    settings = [
        f"restore_command = 'cp {WAL_RESTORE_MOUNT}/%f %p'",
        "recovery_target_timeline = 'latest'",
        "recovery_target_action = 'promote'",
    ]
    if target_time is not None:
        settings.append(f"recovery_target_time = '{iso(target_time)}'")
    conf = "\n".join(["# aicd restore " + backup_id, *settings]) + "\n"
    script = f"cat >> {DATA_MOUNT}/postgresql.auto.conf && touch {DATA_MOUNT}/recovery.signal"
    cmd = ["docker", "run", "-i", "--rm", "--network", "none", "--user", POSTGRES_UID]
    cmd += [*volume_args(data_volume, DATA_MOUNT), image, "sh", "-c", script]
    _pipe_into_container(cmd, iter([conf.encode()]))
    if "backup successfully verified" not in verify:
        raise BackupError(f"pg_verifybackup не подтвердил бэкап {backup_id}: {verify[-800:]}")
    return {"backup_id": backup_id, "pg_verifybackup": verify.strip(), "wal_staged": wal, "recovery": settings}


def _stage_wal(repo: Repository, sysid: str, start_segment: str, volume: str, image: str) -> dict[str, Any]:
    """Расшифрованный архив WAL кластера бэкапа (только его system identifier) — в том для restore_command."""
    names = wal_names(repo, sysid)
    wanted = [n for n in names if not SEGMENT_RE.fullmatch(n) or n[8:] >= start_segment[8:]]

    def tar_stream() -> Iterator[bytes]:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w|") as tar:
            for name in wanted:
                with tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024) as plain:
                    for chunk in decrypt_file(repo.key, f"wal/{sysid}/{name}", wal_dir(repo, sysid) / f"{name}.enc"):
                        plain.write(chunk)
                    info = tarfile.TarInfo(name)
                    info.size = plain.tell()
                    info.mode = 0o600
                    plain.seek(0)
                    tar.addfile(info, plain)
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate()
        yield buffer.getvalue()

    cmd = ["docker", "run", "-i", "--rm", "--network", "none", "--user", "0:0", *volume_args(volume, "/wal"), image]
    cmd += ["sh", "-c", f"tar -xf - -C /wal && chown -R {POSTGRES_UID} /wal && chmod 0700 /wal"]
    _pipe_into_container(cmd, tar_stream())
    segments = [n for n in wanted if SEGMENT_RE.fullmatch(n)]
    return {"files": len(wanted), "segments": len(segments), "last_segment": segments[-1] if segments else None}


def post_restore(target: PgTarget) -> dict[str, Any]:
    """После promote: снять recovery-настройки, сдвинуть последовательность fencing token, факты сервера."""
    in_recovery = psql(target, "SELECT pg_is_in_recovery()")
    if in_recovery != "f":
        raise BackupError("сервер ещё в recovery: дождитесь promote (логи postgres)")
    for name in ("restore_command", "recovery_target_timeline", "recovery_target_action", "recovery_target_time"):
        psql(target, f"ALTER SYSTEM RESET {name}")
    psql(target, "SELECT pg_reload_conf()")
    existing = set(
        psql(target, "SELECT relname FROM pg_class WHERE relnamespace = to_regnamespace('aicd')").split()
    )  # миграции старее 0002/0003 — части таблиц/последовательности нет
    new_value = "0"
    if "worker_lease_fencing_seq" in existing:
        parts = [f"(SELECT coalesce(max({col}), 0) FROM aicd.{table})" for table, col in FENCING_SOURCES if table in existing]
        greatest = f"GREATEST({', '.join([*parts, '(SELECT last_value FROM aicd.worker_lease_fencing_seq)'])})"
        new_value = psql(target, f"SELECT setval('aicd.worker_lease_fencing_seq', {greatest} + {FENCING_BUMP})")
    timeline = psql(target, "SELECT timeline_id FROM pg_control_checkpoint()")
    return {"fencing_seq": int(new_value), "timeline": int(timeline), **server_facts(target)}


@dataclass(frozen=True)
class WalArchiveConfig:
    """Параметры сервера для архивирования WAL (compose.yaml и DR drill используют одни и те же)."""

    archive_timeout_seconds: int = 60

    def server_args(self) -> list[str]:
        command = (
            f"test ! -f {WAL_ARCHIVE_MOUNT}/%f && cp %p {WAL_ARCHIVE_MOUNT}/%f.tmp"
            f" && mv {WAL_ARCHIVE_MOUNT}/%f.tmp {WAL_ARCHIVE_MOUNT}/%f"
        )
        return [
            "-c",
            "wal_level=replica",
            "-c",
            "archive_mode=on",
            "-c",
            f"archive_command={command}",
            "-c",
            f"archive_timeout={self.archive_timeout_seconds}",
            "-c",
            "max_wal_senders=4",
        ]


def check_docker() -> None:
    try:
        docker("version", "--format", "{{.Server.Version}}", timeout=30)
    except (DockerError, FileNotFoundError) as exc:
        raise BackupError(f"Docker недоступен: {exc}") from exc
