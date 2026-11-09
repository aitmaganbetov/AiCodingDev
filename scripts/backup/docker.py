"""Запуск инструментов PostgreSQL в закреплённом по digest образе (тот же, что у сервиса `postgres`).

Клиентские утилиты (`pg_basebackup`, `pg_dump`, `pg_verifybackup`, `psql`) берутся из образа сервера —
версия клиента всегда совпадает с сервером, на хосте оператора нужен только Docker. Данные идут через
stdin/stdout контейнера: на хосте не остаются незашифрованные копии.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from aicd_verification.images import DEFAULT_GATE_IMAGES

# Тот же digest, что у сервиса postgres в compose.yaml и у gate MIGRATION (aicd_verification.images).
POSTGRES_IMAGE = DEFAULT_GATE_IMAGES["postgres"]
POSTGRES_UID = "70:70"  # пользователь postgres в образе postgres:*-alpine


class DockerError(RuntimeError):
    pass


def docker(*args: str, input_bytes: bytes | None = None, check: bool = True, timeout: float = 600) -> str:
    proc = subprocess.run(  # noqa: S603 — фиксированный бинарь docker, аргументы — список
        ["docker", *args],  # noqa: S607
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        raise DockerError(f"docker {args[0]} {args[1] if len(args) > 1 else ''}: {proc.stderr.decode()[-1500:]}")
    return proc.stdout.decode()


@dataclass(frozen=True)
class PgTarget:
    """Подключение к серверу PostgreSQL из вспомогательного контейнера."""

    host: str
    port: int = 5432
    user: str = "aicd_admin"
    password_file: Path | None = None
    dbname: str = "aicd"
    network: str | None = None  # сеть Docker (compose: aicodingdev_backend); None — сеть по умолчанию
    image: str = POSTGRES_IMAGE
    extra_env: dict[str, str] = field(default_factory=dict)
    # Режим `docker exec` в контейнер сервера (unix-сокет, `local … trust` образа postgres): нужен для
    # pg_basebackup — pg_hba образа не разрешает replication-подключения по сети.
    container: str | None = None

    def conn_args(self) -> list[str]:
        if self.container:
            return ["-h", "/var/run/postgresql", "-p", "5432", "-U", self.user]
        return ["-h", self.host, "-p", str(self.port), "-U", self.user]

    def command(self, auth: Sequence[str], argv: Sequence[str], *, interactive: bool = False) -> list[str]:
        """Полная команда docker: exec в контейнер сервера или run вспомогательного контейнера из образа."""
        if self.container:
            return ["docker", "exec", *(["-i"] if interactive else []), "-u", "postgres", self.container, *argv]
        return ["docker", *run_args(self, auth, interactive=interactive), self.image, *argv]


@contextlib.contextmanager
def pgpass(target: PgTarget) -> Iterator[list[str]]:
    """Аргументы `docker run` для пароля: временный `.pgpass` (0600, только чтение), не в env/argv."""
    if target.password_file is None or target.container:
        yield []
        return
    password = target.password_file.read_text(encoding="utf-8").strip()
    with tempfile.TemporaryDirectory(prefix="aicd-pgpass-") as tmp:
        path = Path(tmp) / "pgpass"
        escaped = password.replace("\\", "\\\\").replace(":", "\\:")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"*:*:*:{target.user}:{escaped}\n")
        yield ["-v", f"{path}:/run/aicd/pgpass:ro", "-e", "PGPASSFILE=/run/aicd/pgpass"]


def run_args(target: PgTarget, auth: Sequence[str], *, interactive: bool = False) -> list[str]:
    args = ["run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "--read-only"]
    args += ["--tmpfs", "/tmp", "--security-opt", "no-new-privileges:true", "--cap-drop", "ALL"]  # noqa: S108
    if interactive:
        args.append("-i")
    if target.network:
        args += ["--network", target.network]
    for key, value in target.extra_env.items():
        args += ["-e", f"{key}={value}"]
    return [*args, *auth]


def psql(target: PgTarget, sql: str, *, dbname: str | None = None, timeout: float = 120) -> str:
    """Скалярный/табличный запрос (`psql -At`), ON_ERROR_STOP."""
    argv = ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-At", *target.conn_args(), "-d", dbname or target.dbname, "-c", sql]
    with pgpass(target) as auth:
        return docker(*target.command(auth, argv)[1:], timeout=timeout).strip()


def psql_vars(target: PgTarget, sql: str, variables: dict[str, str], *, timeout: float = 120) -> str:
    """Запрос с параметрами-переменными psql (`:'имя'` — экранирует сам psql): SQL — через stdin, значения — `-v`."""
    argv = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-At", *target.conn_args(), "-d", target.dbname]
    for name, value in variables.items():
        argv += ["-v", f"{name}={value}"]
    argv += ["-f", "-"]
    with pgpass(target) as auth:
        cmd = target.command(auth, argv, interactive=True)
        return docker(*cmd[1:], input_bytes=sql.encode(), timeout=timeout).strip()


@contextlib.contextmanager
def streaming_tool(target: PgTarget, argv: Sequence[str]) -> Iterator[subprocess.Popen[bytes]]:
    """Утилита PostgreSQL в контейнере со stdout в процесс оператора (pg_basebackup -D -, pg_dump)."""
    with pgpass(target) as auth:
        cmd = target.command(auth, argv)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # noqa: S603
        try:
            yield proc
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def volume_args(volume: str, mount: str, *, read_only: bool = False) -> list[str]:
    return ["-v", f"{volume}:{mount}{':ro' if read_only else ''}"]


def volume_exists(volume: str) -> bool:
    return bool(docker("volume", "ls", "-q", "--filter", f"name=^{volume}$").strip())
