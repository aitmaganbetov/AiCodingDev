"""Эмулятор CLI-агента для тестов. НЕ является реализацией AI: только сценарии поведения процесса.

Протокол вывода (JSONL в stdout), понятный FakeOutputParser:
  {"event": "message", "text": "..."}
  {"event": "tool", "tool_kind": "file_write", "phase": "completed", "target": "path"}
  {"event": "result", "claims": [{"type": "...", "summary": "..."}], "usage": {...} | null}
Ошибки — сигнатуры в stderr ("FAKE-AUTH: login required", "FAKE-LIMIT: usage limit, retry after 120s").
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def emit(obj: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--prompt-file", required=True)
    args = parser.parse_args()
    if args.scenario == "ignore_sigterm":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # до первого вывода — без гонки с отменой
    task = json.loads(Path(args.prompt_file).read_text(encoding="utf-8"))
    emit({"event": "message", "text": f"Получена задача: {task['objective'][:200]}"})
    s = args.scenario
    if s == "success_diff":
        Path("FAKE_CHANGE.md").write_text(f"# Изменение\n\n{task['objective']}\n", encoding="utf-8")
        emit({"event": "tool", "tool_kind": "file_write", "phase": "completed", "target": "FAKE_CHANGE.md"})
        emit(
            {
                "event": "result",
                "claims": [{"type": "implementation_completed", "summary": "создан FAKE_CHANGE.md"}],
                "usage": None,
            }
        )
        return 0
    if s == "usage_reported":
        emit(
            {
                "event": "result",
                "claims": [{"type": "analysis_completed"}],
                "usage": {"input_tokens": 120, "output_tokens": 30},
            }
        )
        return 0
    if s == "nonzero_exit":
        sys.stderr.write("fatal: something went wrong\n")
        return 1
    if s == "timeout":
        time.sleep(3600)
        return 0
    if s == "chatty_hang":
        while True:
            emit({"event": "message", "text": "работаю..."})
            time.sleep(0.05)
    if s == "auth_required":
        sys.stderr.write("FAKE-AUTH: login required\n")
        return 3
    if s == "rate_limited":
        sys.stderr.write("FAKE-LIMIT: usage limit reached, retry after 120s\n")
        return 4
    if s == "garbage_output":
        sys.stdout.write("this is not json\n{broken\n")
        return 0
    if s == "secret_leak":
        emit({"event": "message", "text": "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789 password=hunter2hunter2"})
        sys.stderr.write("AKIAABCDEFGHIJKLMNOP\n")
        emit({"event": "result", "claims": [{"type": "analysis_completed"}], "usage": None})
        return 0
    if s == "claims_without_evidence":
        emit({"event": "result", "claims": [{"type": "tests_passed", "summary": "все тесты прошли"}], "usage": None})
        return 0
    if s == "big_output":
        line = "x" * 1000
        for _ in range(5000):
            sys.stdout.write(json.dumps({"event": "message", "text": line}) + "\n")
        sys.stdout.flush()
        emit({"event": "result", "claims": [{"type": "other"}], "usage": None})
        return 0
    if s == "ignore_sigterm":
        time.sleep(3600)
        return 0
    if s == "spawn_child":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
        Path("child.pid").write_text(str(child.pid))
        time.sleep(3600)
        return 0
    if s == "env_dump":
        emit({"event": "message", "text": json.dumps(sorted(os.environ))})
        emit({"event": "result", "claims": [{"type": "other"}], "usage": None})
        return 0
    sys.stderr.write(f"unknown scenario {s}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
