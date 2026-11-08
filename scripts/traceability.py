#!/usr/bin/env python3
"""Матрица трассировки требования → задачи backlog (P1-14, мастер-промпт §5).

Падает, если требование P0/P1 из REQUIREMENTS_REGISTRY не покрыто ни одной задачей backlog,
или задача ссылается на несуществующее требование. `--report <file>` пишет матрицу в Markdown.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs" / "planning" / "REQUIREMENTS_REGISTRY.md"
BACKLOG = ROOT / "docs" / "planning" / "IMPLEMENTATION_BACKLOG.md"
REQ_RE = re.compile(r"\bN?FR-[A-Z]+-\d{2}\b")


def requirements(text: str) -> dict[str, str]:
    reqs: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\| (N?FR-[A-Z]+-\d{2}) \|", line)
        if m:
            cells = [c.strip() for c in line.strip("|").split("|")]
            priority = next((c for c in cells if re.fullmatch(r"P[0-2]( \(.*\))?", c)), "P0")
            reqs[m.group(1)] = priority.split()[0]
    return reqs


def tasks(text: str) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for line in text.splitlines():
        m = re.match(r"^\| (P\d-\d{2}) \|", line)
        # Учитываются только строки таблиц фаз (7 колонок); журнал выполнения повторяет ID задач.
        if m and m.group(1) not in result:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) == 7:
                result[m.group(1)] = {"reqs": set(REQ_RE.findall(line)), "status": cells[-1]}
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report")
    args = parser.parse_args()
    reqs = requirements(REGISTRY.read_text(encoding="utf-8"))
    backlog = tasks(BACKLOG.read_text(encoding="utf-8"))
    covered: dict[str, list[str]] = {r: [] for r in reqs}
    unknown = []
    for task_id, info in backlog.items():
        for r in info["reqs"]:  # type: ignore[attr-defined]
            if r in covered:
                covered[r].append(task_id)
            else:
                unknown.append(f"{task_id} → {r}")
    uncovered = [r for r, ts in covered.items() if not ts and reqs[r] in ("P0", "P1")]
    if args.report:
        lines = ["| Требование | Приоритет | Задачи | Статусы |", "|---|---|---|---|"]
        for r, ts in covered.items():
            statuses = ", ".join(sorted({str(backlog[t]["status"]) for t in ts}))
            lines.append(f"| {r} | {reqs[r]} | {', '.join(ts) or '—'} | {statuses or '—'} |")
        Path(args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"Требований: {len(reqs)}, задач: {len(backlog)}, непокрытых P0/P1: {len(uncovered)}, "
        f"ссылок на неизвестные требования: {len(unknown)}"
    )
    for item in uncovered:
        print(f"  не покрыто: {item}", file=sys.stderr)
    for item in unknown:
        print(f"  неизвестное требование: {item}", file=sys.stderr)
    return 1 if uncovered or unknown else 0


if __name__ == "__main__":
    raise SystemExit(main())
