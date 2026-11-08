"""SCOPE-сканер (QG-SCOPE): исполняется ВНУТРИ verification-контейнера как `python3 -I -S -c <source> <input>`.

Модуль самодостаточен (только stdlib): исходный текст файла целиком передаётся интерпретатору
контейнера, поэтому импортов пакетов платформы здесь быть не должно.

Вход (`scope.json`, готовит платформа на хосте через `git diff` по объектам репозитория — код
репозитория при этом не исполняется):

    {"entries": [{"path", "status", "old_mode", "new_mode", "link_target"}],
     "diff_file": "diff.patch",            # unified diff (-U0) base..commit рядом с scope.json
     "rules": [{"id", "pattern", "flags"}], # секрет-паттерны (redaction платформы + gitleaks-style)
     "allow_substrings": [...]}            # заведомые плейсхолдеры (EXAMPLE, ${...} и т.п.)

Выход: JSON-отчёт в stdout. Значения секретов НИКОГДА не печатаются — только id правила, путь и
номер строки. Код выхода: 0 — нарушений нет, 1 — есть нарушения, 2 — некорректный вход.
"""

from __future__ import annotations

import json
import posixpath
import re
import sys
from pathlib import Path
from typing import Any

SCANNER_VERSION = "1"
MAX_REPORTED = 200
MAX_LINE_CHARS = 20000  # длинные (минифицированные) строки сканируются по префиксу
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
SECRET_SEVERITY = "critical"  # noqa: S105 — уровень находки, не секрет
PATH_SEVERITY = "high"


def _path_violations(entry: dict[str, Any]) -> list[str]:
    rules: list[str] = []
    path = str(entry.get("path", ""))
    parts = path.split("/")
    if not path or path.startswith(("/", "\\")):
        rules.append("path_absolute")
    if ".." in parts:
        rules.append("path_traversal")
    if any(p.casefold() == ".git" for p in parts):
        rules.append("git_internal_path")
    new_mode = str(entry.get("new_mode", ""))
    if new_mode == "160000":
        rules.append("submodule_change")
    if new_mode == "120000":
        target = entry.get("link_target")
        if not isinstance(target, str) or not target:
            rules.append("symlink_unreadable")
        else:
            if target.startswith("/"):
                rules.append("symlink_outside_worktree")
            else:
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
                if resolved == ".." or resolved.startswith("../"):
                    rules.append("symlink_outside_worktree")
                elif any(p.casefold() == ".git" for p in resolved.split("/")):
                    rules.append("symlink_into_git_dir")
    return rules


def _added_lines(diff_text: str) -> list[tuple[str, int, str]]:
    """(путь, номер строки в новой версии, текст) для каждой добавленной строки unified diff."""
    out: list[tuple[str, int, str]] = []
    path = "?"
    line_no = 0
    in_hunk = False
    for raw in diff_text.split("\n"):
        if raw.startswith("diff --git "):
            in_hunk = False
            path = "?"
            continue
        if not in_hunk:
            if raw.startswith("+++ "):
                target = raw[4:]
                path = target[2:] if target.startswith("b/") else ("?" if target == "/dev/null" else target)
                continue
            match = _HUNK_RE.match(raw)
            if match:
                in_hunk = True
                line_no = int(match.group(1))
            continue
        match = _HUNK_RE.match(raw)
        if match:
            line_no = int(match.group(1))
            continue
        if raw.startswith("+"):
            out.append((path, line_no, raw[1:MAX_LINE_CHARS]))
            line_no += 1
        elif raw.startswith(" "):
            line_no += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            continue
        else:
            in_hunk = False  # конец хунка (служебные строки следующего файла)
    return out


def scan(spec: dict[str, Any], diff_text: str) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    entries = spec.get("entries", [])
    for entry in entries:
        for rule in _path_violations(entry):
            violations.append({"rule": rule, "severity": PATH_SEVERITY, "path": entry.get("path"), "line": None})
    rules = [(str(r["id"]), re.compile(str(r["pattern"]), int(r.get("flags", 0)))) for r in spec.get("rules", [])]
    allow = [str(a) for a in spec.get("allow_substrings", [])]
    added = _added_lines(diff_text)
    for path, line_no, text in added:
        for rule_id, pattern in rules:
            hit = False
            for match in pattern.finditer(text):
                if not any(a in match.group(0) for a in allow):
                    hit = True
                    break
            if hit:
                # Только метаданные: значение совпадения не выводится никогда.
                violations.append(
                    {"rule": f"secret-scan/{rule_id}", "severity": SECRET_SEVERITY, "path": path, "line": line_no}
                )
                break  # одна находка на строку
    return {
        "tool": "aicd-scope",
        "version": SCANNER_VERSION,
        "files_changed": len(entries),
        "added_lines_scanned": len(added),
        "violations_total": len(violations),
        "violations": violations[:MAX_REPORTED],
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(json.dumps({"tool": "aicd-scope", "error": "usage: scope_scan <scope.json>"}))
        return 2
    try:
        spec_path = Path(argv[1])
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        diff_name = str(spec.get("diff_file", ""))
        if not diff_name or "/" in diff_name:
            raise ValueError("diff_file must be a bare file name")
        diff_text = (spec_path.parent / diff_name).read_bytes().decode("utf-8", errors="replace")
        report = scan(spec, diff_text)
    except (OSError, ValueError, KeyError, TypeError, re.error) as exc:
        print(json.dumps({"tool": "aicd-scope", "error": type(exc).__name__}))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if report["violations_total"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
