"""Измеритель PERF gate (P6-06): исполняется ВНУТРИ verification-контейнера, только stdlib.

Запуск: `python3 -I -S -c <source> /aicd-input/bench.json`. Спецификация (готовит платформа):
`{"command": [...], "repeat": N, "warmup": W, "run_timeout_seconds": T,
  "variants": {"baseline": "/baseline", "candidate": "/workspace"}}`.

Команда (argv без shell) выполняется в каталоге каждого варианта: сначала `warmup` прогонов на вариант,
затем N пар прогонов с чередованием порядка (ABBA), чтобы дрейф окружения (прогрев кэшей, троттлинг)
не попадал систематически в один вариант. Время — `time.perf_counter_ns` вокруг `subprocess.run`.
Результат — один JSON на stdout: выборки, коды выхода, окружение. Код выхода 0 — измерение
выполнено (провалы команды отражены в отчёте), 2 — неверная спецификация.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time

HARNESS = "aicd-bench/1"
MAX_STDERR = 2000


def _fail(message: str) -> int:
    print(json.dumps({"harness": HARNESS, "error": message}))
    return 2


def _env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),  # noqa: S108 — tmpfs контейнера
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
    }


def _run_once(argv: list[str], cwd: str, timeout: float) -> tuple[int, int, str]:
    started = time.perf_counter_ns()
    try:
        proc = subprocess.run(  # noqa: S603 — argv из спецификации платформы, без shell
            argv,
            cwd=cwd,
            env=_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return -1, time.perf_counter_ns() - started, "timeout"
    except OSError as exc:
        return -2, time.perf_counter_ns() - started, f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter_ns() - started
    tail = proc.stderr.decode("utf-8", "replace")[-MAX_STDERR:] if proc.returncode else ""
    return proc.returncode, elapsed, tail


def main(spec_path: str) -> int:
    try:
        with open(spec_path, encoding="utf-8") as fh:
            spec = json.load(fh)
        argv = [str(a) for a in spec["command"]]
        repeat = int(spec["repeat"])
        warmup = int(spec.get("warmup", 1))
        timeout = float(spec.get("run_timeout_seconds", 60))
        variants = dict(spec["variants"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _fail(f"invalid bench spec: {type(exc).__name__}")
    if not argv or repeat < 1 or not variants:
        return _fail("invalid bench spec: empty command/variants")
    for name, path in variants.items():
        if not os.path.isdir(path):
            return _fail(f"variant {name}: directory {path} is missing")
    names = sorted(variants)
    samples: dict[str, list[int]] = {n: [] for n in names}
    codes: dict[str, list[int]] = {n: [] for n in names}
    warm_codes: dict[str, list[int]] = {n: [] for n in names}
    tails: dict[str, str] = dict.fromkeys(names, "")

    def record(name: str, *, warm: bool) -> None:
        code, elapsed, tail = _run_once(argv, variants[name], timeout)
        if warm:
            warm_codes[name].append(code)
        else:
            samples[name].append(elapsed)
            codes[name].append(code)
        if tail and not tails[name]:
            tails[name] = tail

    for _ in range(warmup):
        for name in names:
            record(name, warm=True)
    order: list[str] = []
    for i in range(repeat):
        pair = names if i % 2 == 0 else list(reversed(names))
        for name in pair:
            record(name, warm=False)
            order.append(name)
    results = {
        n: {
            "samples_ns": samples[n],
            "exit_codes": codes[n],
            "warmup_exit_codes": warm_codes[n],
            "stderr_tail": tails[n],
        }
        for n in names
    }
    load = os.getloadavg() if hasattr(os, "getloadavg") else None
    report = {
        "harness": HARNESS,
        "error": None,
        "command": argv,
        "repeat": repeat,
        "warmup": warmup,
        "order": order,
        "variants": results,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "sched_cpus": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
            "loadavg": list(load) if load else None,
            "clock": "time.perf_counter_ns",
        },
    }
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]) if len(sys.argv) == 2 else _fail("usage: bench_runner <spec.json>"))
