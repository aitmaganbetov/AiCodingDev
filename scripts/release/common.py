"""Общие части конвейера релиза: закреплённые образы syft/cosign, запуск в контейнере, хэши."""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from aicd_verification.tools.images import COSIGN, SYFT

ROOT = Path(__file__).resolve().parents[2]
SLSA_PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
BUILD_TYPE = "https://aicodingdev.local/release/docker-build/v1"
IMAGES = {
    "backend": "infrastructure/docker/backend.Dockerfile",
    "web": "infrastructure/docker/web.Dockerfile",
}


class ReleaseError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def run(
    cmd: Sequence[str], *, timeout: float = 1800, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — аргументы списком
        list(cmd), capture_output=True, text=True, timeout=timeout, check=False, env=env
    )


def must(cmd: Sequence[str], *, timeout: float = 1800, env: dict[str, str] | None = None) -> str:
    proc = run(cmd, timeout=timeout, env=env)
    if proc.returncode != 0:
        raise ReleaseError(f"{' '.join(cmd[:4])}… → {proc.returncode}: {proc.stderr[-2000:]}")
    return proc.stdout


def tool_container(image: str, workdir: Path, args: Sequence[str], *, env: dict[str, str] | None = None,
                   read_only_mounts: dict[Path, str] | None = None) -> list[str]:  # fmt: skip
    """`docker run` инструмента без сети, read-only rootfs, от пользователя хоста; каталог работы — /w."""
    cmd = ["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL"]  # noqa: S108
    cmd += ["--security-opt", "no-new-privileges:true", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp"]
    for key, value in (env or {}).items():
        cmd += ["-e", f"{key}={value}"]
    cmd += ["-w", "/w", "-v", f"{workdir}:/w"]
    for host, mount in (read_only_mounts or {}).items():
        cmd += ["-v", f"{host}:{mount}:ro"]
    return [*cmd, image, *args]


def cosign(workdir: Path, args: Sequence[str], *, env: dict[str, str] | None = None,
           mounts: dict[Path, str] | None = None) -> subprocess.CompletedProcess[str]:  # fmt: skip
    return run(tool_container(COSIGN.ref, workdir, args, env=env, read_only_mounts=mounts), timeout=600)


def syft(workdir: Path, source: str, output: str, *, mounts: dict[Path, str] | None = None) -> None:
    cmd = tool_container(SYFT.ref, workdir, ["scan", source, "-o", f"cyclonedx-json={output}"],
                         env={"SYFT_CHECK_FOR_APP_UPDATE": "false"}, read_only_mounts=mounts)  # fmt: skip
    proc = run(cmd, timeout=1800)
    if proc.returncode != 0:
        raise ReleaseError(f"syft {source}: {proc.stderr[-1500:]}")


OFFLINE_SIGN = ("--tlog-upload=false", "--use-signing-config=false", "--yes")
OFFLINE_VERIFY = ("--insecure-ignore-tlog=true",)
