"""Драйвер COVERAGE (pytest + coverage.py): исполняется ВНУТРИ verification-контейнера как
`python3 -I -S -c <source> <nonce>` (только stdlib, без импортов платформы).

1. Запускает `python3 -m pytest … --cov=. --cov-report=json:<tmp>/coverage.json` в текущем каталоге
   (рабочее дерево read-only: данные coverage — в `COVERAGE_FILE` во временном каталоге tmpfs).
2. Отчёт принимается, только если файл создан этим прогоном (mtime не раньше старта): подложенный
   в репозиторий/окружение отчёт не засчитывается (FP-08).
3. Последней строкой печатает `AICD_COVERAGE_REPORT <nonce> <json|null>` — по файлам
   `[covered_lines, num_statements]`; код выхода — код pytest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

MAX_FILES = 20000


def main(argv: list[str]) -> int:
    nonce = argv[1] if len(argv) > 1 else ""
    out_dir = tempfile.mkdtemp(prefix="aicd-cov-")
    report_path = os.path.join(out_dir, "coverage.json")
    env = dict(os.environ)
    env["COVERAGE_FILE"] = os.path.join(out_dir, ".coverage")
    started = time.time()
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--color=no",
        "--cov=.",
        "--cov-report=json:" + report_path,
    ]
    sys.stdout.flush()
    code = subprocess.call(cmd, env=env)  # noqa: S603 — фиксированный argv, без shell
    summary = None
    try:
        if os.stat(report_path).st_mtime >= started - 1:
            with open(report_path, encoding="utf-8") as fh:
                data = json.load(fh)
            files = {}
            for path, info in list((data.get("files") or {}).items())[:MAX_FILES]:
                s = info.get("summary") or {}
                files[path] = [int(s.get("covered_lines", 0)), int(s.get("num_statements", 0))]
            totals = data.get("totals") or {}
            summary = {
                "files": files,
                "totals": [int(totals.get("covered_lines", 0)), int(totals.get("num_statements", 0))],
                "tool": (data.get("meta") or {}).get("version"),
            }
    except (OSError, ValueError, TypeError, AttributeError):
        summary = None
    sys.stdout.flush()
    print("AICD_COVERAGE_REPORT " + nonce + " " + json.dumps(summary, separators=(",", ":")), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
