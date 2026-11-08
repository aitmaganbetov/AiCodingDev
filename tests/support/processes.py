"""Настоящие процессы приложения для chaos-тестов P9-03: API (uvicorn) и воркеры (`aicd_worker.main`),
частный `redis-server`. Тест убивает их SIGKILL и перезапускает; общие контейнеры не трогаются."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_port(port: int, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise AssertionError(f"порт {port} не открылся за {seconds} с")


class RedisProcess:
    """`redis-server` без persistence на своём порту: SIGKILL теряет всё содержимое (очереди, pub/sub)."""

    def __init__(self, port: int | None = None) -> None:
        self.port = port or free_port()
        self.proc: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self.port}/0"

    def start(self) -> None:
        self.proc = subprocess.Popen(
            ["redis-server", "--port", str(self.port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wait_port(self.port, 10)

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)


class AppProcess:
    """Процесс приложения с окружением `AICD_*`; `kill()` — SIGKILL без штатной остановки."""

    def __init__(self, argv: list[str], env: Mapping[str, str], *, port: int | None = None) -> None:
        self.argv = argv
        self.env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), **env}
        self.port = port
        self.proc: subprocess.Popen[bytes] | None = None
        # stderr — в файл, а не в pipe: долгоживущий процесс иначе блокируется на записи логов в заполненный pipe.
        self.log = Path(tempfile.mkstemp(prefix="aicd-proc-", suffix=".log")[1])

    def start(self) -> None:
        with self.log.open("ab") as err:
            self.proc = subprocess.Popen(self.argv, cwd=REPO_ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=err)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stderr_tail(self) -> str:
        try:
            return self.log.read_text(errors="replace")[-3000:]
        except OSError:
            return ""

    def kill(self) -> None:
        if self.alive():
            assert self.proc is not None
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(timeout=10)

    def stop(self) -> None:
        if self.alive():
            assert self.proc is not None
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.kill()


def api_process(env: Mapping[str, str], port: int | None = None) -> AppProcess:
    port = port or free_port()
    argv = [sys.executable, "-m", "uvicorn", "aicd_api.main:create_app", "--factory", "--host", "127.0.0.1",
            "--port", str(port), "--log-level", "warning", "--no-access-log"]  # fmt: skip
    return AppProcess(argv, env, port=port)


def wait_ready(base_url: str, seconds: float = 30.0, proc: AppProcess | None = None) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc is not None and not proc.alive() and proc.proc is not None:
            raise AssertionError(f"процесс API завершился: {proc.stderr_tail()}")
        try:
            if httpx.get(f"{base_url}/api/v1/health/live", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError("API не поднялся")


def worker_process(kind: str, env: Mapping[str, str]) -> AppProcess:
    return AppProcess([sys.executable, "-m", "aicd_worker.main", kind], env)
