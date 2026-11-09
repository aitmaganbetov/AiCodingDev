"""P9-10: подтверждение «coverage core ≥ 85%» и «100% обязательных тестов» — отчёт из результатов прогона.

    uv run python scripts/coverage_report.py --run      # полный pytest --cov + junit, затем отчёт
    uv run python scripts/coverage_report.py            # отчёт из reports/coverage/{coverage.json,junit-full.xml}

Состав core и исключения — coverage-exceptions.yaml (OQ-15, вариант (a)). Метрика — line coverage
(QUALITY_GATES §6), branch coverage показывается рядом. Порог проверяется для каждого core-пакета и для core в
целом; отчёт приводит числа и С утверждаемыми исключениями, и БЕЗ них. Тесты: каждый собранный тест — PASSED,
FAILED или SKIPPED (с причиной); SKIPPED ≠ PASS и перечисляется отдельно. Код выхода 0 — только если все core-пакеты
≥ порога (с исключениями) и нет FAILED.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "coverage"
EXCEPTIONS = ROOT / "coverage-exceptions.yaml"


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False).stdout.strip()  # noqa: S603, S607


def run_suite(out: Path, extra: list[str]) -> dict[str, Any]:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        "-rs",
        "--cov",
        f"--cov-report=json:{out / 'coverage.json'}",
        "--cov-report=term",
        "-o",
        "junit_family=xunit1",
        f"--junitxml={out / 'junit-full.xml'}",
        *extra,
    ]
    started = datetime.now(UTC)
    t0 = time.monotonic()
    with (out / "pytest-full.log").open("w", encoding="utf-8") as fh:
        code = subprocess.run(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, check=False).returncode  # noqa: S603
    run = {
        "command": "uv run " + " ".join(["pytest", *cmd[3:]]).replace(str(ROOT) + "/", ""),
        "exit_code": code,
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "duration_seconds": round(time.monotonic() - t0, 1),
    }
    (out / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return run


def package_of(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[:2]) if parts[0] in ("packages", "apps") and len(parts) > 2 else parts[0]


def aggregate(files: dict[str, Any], exclude: set[str]) -> dict[str, dict[str, Any]]:
    agg: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])
    for path, info in files.items():
        if path in exclude:
            continue
        s = info["summary"]
        a = agg[package_of(path)]
        a[0] += s["num_statements"]
        a[1] += s["covered_lines"]
        a[2] += s.get("num_branches", 0)
        a[3] += s.get("covered_branches", 0)
        a[4] += 1
    out: dict[str, dict[str, Any]] = {}
    for pkg, (st, cl, br, cb, n) in sorted(agg.items()):
        out[pkg] = {
            "files": n,
            "statements": st,
            "covered_lines": cl,
            "line_percent": round(100 * cl / st, 2) if st else 100.0,
            "branches": br,
            "covered_branches": cb,
            "branch_percent": round(100 * cb / br, 2) if br else 100.0,
            "combined_percent": round(100 * (cl + cb) / (st + br), 2) if st + br else 100.0,
        }
    return out


def total(pkgs: dict[str, dict[str, Any]], names: list[str]) -> dict[str, Any]:
    st = sum(pkgs[n]["statements"] for n in names if n in pkgs)
    cl = sum(pkgs[n]["covered_lines"] for n in names if n in pkgs)
    br = sum(pkgs[n]["branches"] for n in names if n in pkgs)
    cb = sum(pkgs[n]["covered_branches"] for n in names if n in pkgs)
    return {
        "statements": st,
        "covered_lines": cl,
        "line_percent": round(100 * cl / st, 2) if st else 100.0,
        "branches": br,
        "covered_branches": cb,
        "branch_percent": round(100 * cb / br, 2) if br else 100.0,
    }


def weakest(files: dict[str, Any], core: list[str], exclude: set[str], limit: int = 15) -> list[dict[str, Any]]:
    rows = []
    for path, info in files.items():
        if path in exclude or package_of(path) not in core:
            continue
        s = info["summary"]
        missing = s["num_statements"] - s["covered_lines"]
        if missing:
            rows.append({"path": path, "missing_lines": missing, "line_percent": round(s["percent_covered"], 1)})
    return sorted(rows, key=lambda r: -r["missing_lines"])[:limit]


def tests_summary(junit: Path) -> dict[str, Any]:
    counts = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
    failed: list[str] = []
    skipped: list[dict[str, str]] = []
    for tc in ET.parse(junit).getroot().iter("testcase"):  # noqa: S314 — файл создан этим прогоном
        counts["total"] += 1
        node = f"{tc.get('file') or tc.get('classname')}::{tc.get('name')}"
        if tc.find("failure") is not None or tc.find("error") is not None:
            counts["failed"] += 1
            failed.append(node)
        elif (sk := tc.find("skipped")) is not None:
            counts["skipped"] += 1
            skipped.append({"test": node, "reason": (sk.get("message") or "").strip()[:300]})
        else:
            counts["passed"] += 1
    return {
        **counts,
        "failed_tests": failed,
        "skipped_tests": skipped,
        "junit_sha256": hashlib.sha256(junit.read_bytes()).hexdigest(),
    }


def render(doc: dict[str, Any]) -> str:
    th = doc["threshold_percent"]
    t = doc["tests"]
    run = doc.get("run") or {}
    lines = [
        "# Coverage report — core ≥ 85% и 100% обязательных тестов (P9-10)",
        "",
        "> Сгенерировано `scripts/coverage_report.py` из `coverage.json` и junit полного прогона — не редактировать вручную.",
        "",
        f"- **Дата (UTC):** {doc['generated_at']}; **commit:** `{doc['commit_sha']}`"
        + (
            f"; рабочее дерево изменено ({doc['dirty_paths']} путей)"
            if doc["dirty_paths"]
            else "; рабочее дерево чистое"
        ),
        f"- **Команда:** `{run.get('command', '—')}` → exit {run.get('exit_code', '—')}, "
        f"{run.get('duration_seconds', '—')} с (начало {run.get('started_at', '—')})",
        f"- **Инструмент:** coverage.py {doc['coverage_tool']} (branch=true); метрика порога — {doc['metric']} coverage",
        f"- **Core (OQ-15 (a), `coverage-exceptions.yaml`):** {', '.join(f'`{c}`' for c in doc['core_packages'])}",
        f"- **Тесты:** {t['passed']} passed, {t['failed']} failed, {t['skipped']} skipped из {t['total']} "
        f"(junit sha256 `{t['junit_sha256'][:16]}…`)",
        f"- **Итог core:** {doc['core_total_with_exceptions']['line_percent']}% line (с исключениями), "
        f"{doc['core_total_without_exceptions']['line_percent']}% (без исключений); порог {th}% — "
        + ("**выполнен для каждого core-пакета**" if doc["core_pass"] else "**НЕ выполнен** (см. таблицу)"),
        "",
        "## Core-пакеты",
        "",
        "| Пакет | Файлов | Line, % (с искл.) | Line, % (без искл.) | Branch, % | Statements (покрыто) | ≥ порога |",
        "|---|---|---|---|---|---|---|",
    ]
    for pkg in doc["core_packages"]:
        w, wo = doc["packages_with_exceptions"].get(pkg), doc["packages_without_exceptions"].get(pkg)
        if not w:
            continue
        ok = "да" if w["line_percent"] >= th else "**нет**"
        lines.append(
            f"| `{pkg}` | {w['files']} | {w['line_percent']} | {wo['line_percent']} | {w['branch_percent']} | "
            f"{w['statements']} ({w['covered_lines']}) | {ok} |"
        )
    c = doc["core_total_with_exceptions"]
    lines += [
        f"| **core всего** |  | **{c['line_percent']}** | {doc['core_total_without_exceptions']['line_percent']} | "
        f"{c['branch_percent']} | {c['statements']} ({c['covered_lines']}) | {'да' if c['line_percent'] >= th else '**нет**'} |",
        "",
        "## Остальные пакеты (не core, справочно)",
        "",
        "| Пакет | Line, % | Branch, % | Statements |",
        "|---|---|---|---|",
    ]
    for pkg, v in doc["packages_with_exceptions"].items():
        if pkg not in doc["core_packages"]:
            lines.append(f"| `{pkg}` | {v['line_percent']} | {v['branch_percent']} | {v['statements']} |")
    a = doc["all_total"]
    lines += [
        f"| **всё** | **{a['line_percent']}** | {a['branch_percent']} | {a['statements']} |",
        "",
        "## Исключения (coverage-exceptions.yaml)",
        "",
        "| Файл | Статус | Обоснование |",
        "|---|---|---|",
    ]
    for e in doc["exceptions"]:
        lines.append(f"| `{e['path']}` | {e['status']} | {' '.join(str(e['reason']).split())} |")
    lines += [
        "",
        "## Наименее покрытые файлы core (по числу непокрытых строк)",
        "",
        "| Файл | Непокрыто строк | Line, % |",
        "|---|---|---|",
    ]
    for r in doc["weakest_core_files"]:
        lines.append(f"| `{r['path']}` | {r['missing_lines']} | {r['line_percent']} |")
    lines += ["", "## Обязательные тесты", ""]
    if t["failed_tests"]:
        lines += ["**FAILED:**", ""] + [f"- `{x}`" for x in t["failed_tests"]] + [""]
    else:
        lines += ["FAILED: нет.", ""]
    if t["skipped_tests"]:
        lines += ["**SKIPPED (не засчитываются как PASS):**", ""]
        lines += [f"- `{x['test']}` — {x['reason']}" for x in t["skipped_tests"]]
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--run", action="store_true", help="выполнить полный pytest --cov перед отчётом")
    p.add_argument("--out", type=Path, default=OUT)
    p.add_argument("pytest_args", nargs="*")
    args = p.parse_args(argv)
    out: Path = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.run:
        if not os.environ.get("AICD_TEST_DATABASE_ADMIN_URL"):
            print("ВНИМАНИЕ: AICD_TEST_DATABASE_ADMIN_URL не задан — тесты с БД будут SKIPPED", file=sys.stderr)
        run_suite(out, args.pytest_args)
    run = json.loads((out / "run.json").read_text()) if (out / "run.json").exists() else None
    cfg = yaml.safe_load(EXCEPTIONS.read_text(encoding="utf-8"))
    core: list[str] = cfg["core_packages"]
    th = float(cfg["threshold_percent"])
    excluded = {e["path"] for e in cfg["exceptions"]}
    cov = json.loads((out / "coverage.json").read_text(encoding="utf-8"))
    files = cov["files"]
    with_exc = aggregate(files, excluded)
    without = aggregate(files, set())
    core_w = total(with_exc, core)
    doc: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "commit_sha": _git("rev-parse", "HEAD"),
        "dirty_paths": len([x for x in _git("status", "--porcelain").splitlines() if x]),
        "run": run,
        "coverage_tool": cov.get("meta", {}).get("version"),
        "coverage_json_sha256": hashlib.sha256((out / "coverage.json").read_bytes()).hexdigest(),
        "metric": cfg.get("metric", "line"),
        "threshold_percent": th,
        "core_packages": core,
        "exceptions": cfg["exceptions"],
        "packages_with_exceptions": with_exc,
        "packages_without_exceptions": without,
        "core_total_with_exceptions": core_w,
        "core_total_without_exceptions": total(without, core),
        "all_total": total(without, list(without)),
        "core_pass": all(with_exc[c]["line_percent"] >= th for c in core if c in with_exc)
        and core_w["line_percent"] >= th,
        "weakest_core_files": weakest(files, core, excluded),
        "tests": tests_summary(out / "junit-full.xml"),
    }
    (out / "coverage-summary.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "COVERAGE_REPORT.md").write_text(render(doc), encoding="utf-8")
    print(
        f"core line {core_w['line_percent']}% (порог {th}%), pass={doc['core_pass']}; tests "
        f"{doc['tests']['passed']} passed / {doc['tests']['failed']} failed / {doc['tests']['skipped']} skipped"
    )
    for c in core:
        if c in with_exc:
            print(f"  {c}: {with_exc[c]['line_percent']}%")
    return 0 if doc["core_pass"] and doc["tests"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
