"""Запуск служебных команд официального CLI (вход, статус входа) в одноразовом контейнере с томом
учётных данных провайдера (ADR-0010 п.3, SECURITY_ARCHITECTURE §6.2, cli-auth.md §4).

Зачем. В Docker-режиме учётные данные CLI живут только в именованном томе `aicd-cred-<provider>`,
который монтируется в runner того же провайдера как домашний каталог. Чтобы CLI исполнения нашёл
вход, команда входа и status-команда должны выполняться над **тем же томом** — на хосте они писали
и читали бы другой credential store.

Команда запускается клиентом `docker run` (argv-массив, без shell). Это не `DockerSandboxRunner`:
вход интерактивен и требует TTY и stdin, которые политика runner-контейнеров запрещает
(`container_policy`: «Tty/OpenStdin are not allowed»). Поэтому hardening задаётся здесь же флагами и
совпадает с политикой во всём, кроме TTY: non-root, `cap-drop ALL`, `no-new-privileges`, read-only
rootfs, tmpfs `/tmp`, лимиты памяти и pids, явная сеть (не `host`/`bridge`) и **единственное**
монтирование — том учётных данных. Worktree, context pack и сокет Docker в контейнер не попадают.

Модуль чистый: строит argv, ничего не исполняет и не читает содержимое тома.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Final

CREDENTIAL_VOLUME_PREFIX: Final = "aicd-cred-"
CONTAINER_HOME: Final = "/home/agent"
FORBIDDEN_NETWORKS: Final = frozenset({"host", "bridge", "default"})
_VOLUME_RE = re.compile(r"^aicd-cred-[a-z0-9][a-z0-9_.-]{0,100}$")
_NETWORK_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}$")
_USER_RE = re.compile(r"^[1-9][0-9]{0,9}:[0-9]{1,10}$")  # uid 0 (root) не допускается


class LauncherError(ValueError):
    """Параметры запуска нельзя превратить в безопасную команду."""


@dataclass(frozen=True)
class CredentialLauncher:
    """argv-префикс `docker run … <образ>`; за ним идёт путь CLI в образе и его аргументы."""

    argv: tuple[str, ...]
    container_name: str
    docker: str

    def cleanup_argv(self) -> tuple[str, ...]:
        """Снять контейнер, если клиент `docker run` завершился раньше него (отмена, таймаут)."""
        return (self.docker, "rm", "--force", self.container_name)


def credential_volume(provider: str) -> str:
    return f"{CREDENTIAL_VOLUME_PREFIX}{provider}"


def credential_launcher(
    *,
    docker: str,
    image: str,
    volume: str,
    network: str,
    user: str,
    purpose: str,
    interactive: bool,
    home: str = CONTAINER_HOME,
) -> CredentialLauncher:
    """Команда запуска одноразового контейнера с томом учётных данных в домашнем каталоге."""
    if not _VOLUME_RE.fullmatch(volume):
        raise LauncherError(f"том учётных данных {volume!r} должен называться aicd-cred-<provider>")
    if network in FORBIDDEN_NETWORKS or not _NETWORK_RE.fullmatch(network):
        raise LauncherError(f"сеть {network!r} недопустима для контейнера CLI")
    if not _USER_RE.fullmatch(user):
        raise LauncherError(f"пользователь контейнера {user!r} должен быть non-root uid:gid")
    if not image or image.startswith("-") or any(c.isspace() for c in image):
        raise LauncherError("некорректная ссылка на образ CLI")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", purpose):
        raise LauncherError("некорректное назначение контейнера")
    name = f"aicd-cli-{purpose}-{secrets.token_hex(6)}"
    argv: list[str] = [docker, "run", "--rm", "--name", name]
    if interactive:
        argv += ["--interactive", "--tty"]  # вход: device code печатается только в TTY, код уходит в stdin
    argv += [
        "--user", user,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m",  # noqa: S108 — tmpfs контейнера
        "--memory", "1g",
        "--memory-swap", "1g",
        "--pids-limit", "256",
        "--network", network,
        "--mount", f"type=volume,source={volume},target={home},volume-nocopy",
        "--workdir", home,
        "--env", f"HOME={home}",
        "--env", "LANG=C.UTF-8",
        "--env", "TERM=dumb",
        "--env", "NO_COLOR=1",
        "--label", "aicd.managed=true",
        "--label", f"aicd.kind=cli-{purpose}",
        "--label", f"aicd.execution_id={name}",
        image,
    ]  # fmt: skip
    return CredentialLauncher(tuple(argv), name, docker)
