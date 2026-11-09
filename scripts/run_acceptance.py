"""P9-01: единая точка запуска приёмочного набора AC-01…AC-16 и генерация evidence-отчёта.

Запуск (из корня репозитория, нужны PostgreSQL 17 / Redis / S3 тестового окружения — см. README «Тесты»):

    uv run python scripts/run_acceptance.py                 # pytest -m acceptance + web live + web mocked
    uv run python scripts/run_acceptance.py --skip-web      # только pytest (web-пункты будут NOT_RUN)
    uv run python scripts/run_acceptance.py --report-only   # пересобрать отчёт из уже сохранённых junit

Результаты берутся только из junit-отчётов (pytest `--junitxml`, Playwright `junit` reporter), сохранённых в
`reports/acceptance/junit/`; отчёт `reports/acceptance/ACCEPTANCE_REPORT.md` и `acceptance.json` содержат
commit SHA, состояние рабочего дерева, время, команды, коды выхода, sha256 junit-файлов и статус каждого пункта
матрицы (tests/acceptance/matrix.yaml). Правила: тест, не найденный в junit, — MISSING; SKIPPED ≠ PASS;
пункты real-CLI — BLOCKED с причиной (opt-in прогон невозможен, P2-01/P2-13). Код выхода 0 — только если все
16 AC в статусе PASS (по Fake-части).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.acceptance import matrix as acceptance_matrix  # noqa: E402

WEB = ROOT / "apps" / "web"
DEFAULT_OUT = ROOT / "reports" / "acceptance"
SUITES = ("python", "live", "mocked")


@dataclass
class Case:
    suite: str
    key: str  # python: node id; web: "<file>::<title>"
    file: str
    title: str
    status: str  # PASSED | FAILED | SKIPPED
    time: float
    message: str


# ---------------------------------------------------------------------------------------------- запуск


def _run(cmd: list[str], cwd: Path, env: dict[str, str], log: Path) -> dict[str, Any]:
    started = datetime.now(UTC)
    t0 = time.monotonic()
    with log.open("w", encoding="utf-8") as fh:
        proc = subprocess.run(cmd, cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT, check=False)  # noqa: S603
    return {
        "command": " ".join(cmd),
        "cwd": str(cwd.relative_to(ROOT)) or ".",
        "exit_code": proc.returncode,
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "duration_seconds": round(time.monotonic() - t0, 1),
        "log": str(log.relative_to(ROOT)),
    }


def run_python(out: Path, extra: list[str]) -> dict[str, Any]:
    junit = out / "junit" / "python.xml"
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-m",
        "acceptance",
        "-p",
        "no:cacheprovider",
        "-q",
        "-rs",
        "-o",
        "junit_family=xunit1",
        f"--junitxml={junit}",
        *extra,
    ]
    return _run(cmd, ROOT, dict(os.environ), out / "logs" / "python.log")


def _web_env(junit: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PLAYWRIGHT_JUNIT_OUTPUT_NAME"] = str(junit)
    env.setdefault("CI", "1")  # forbidOnly, без reuse чужого сервера
    return env


def run_live(out: Path) -> dict[str, Any]:
    junit = out / "junit" / "web-live.xml"
    env = _web_env(junit)
    env["AICD_E2E_LIVE_STACK"] = "1"
    for name in ("ENDPOINT", "ACCESS_KEY", "SECRET_KEY"):  # S3 стенда = S3 тестового окружения, если не задано
        if f"AICD_TEST_S3_{name}" in env:
            env.setdefault(f"AICD_E2E_S3_{name}", env[f"AICD_TEST_S3_{name}"])
    cmd = [
        "pnpm",
        "exec",
        "playwright",
        "test",
        "-c",
        "playwright.live.config.ts",
        "--trace",
        "off",
        "--reporter=list,junit",
    ]
    return _run(cmd, WEB, env, out / "logs" / "web-live.log")


def run_mocked(out: Path) -> dict[str, Any]:
    junit = out / "junit" / "web-mocked.xml"
    env = _web_env(junit)
    env.setdefault("AICD_E2E_PORT", "4191")
    env.setdefault("AICD_E2E_PERF", "1")  # замер CLI 10 000 строк (порог ≥ 50 fps) — в составе прогона, не пропуск
    cmd = [
        "pnpm",
        "exec",
        "playwright",
        "test",
        "-c",
        "playwright.mocked.config.ts",
        "--trace",
        "off",
        "--reporter=list,junit",
    ]
    return _run(cmd, WEB, env, out / "logs" / "web-mocked.log")


# ---------------------------------------------------------------------------------------------- junit


def _status(tc: ET.Element) -> tuple[str, str]:
    for tag, status in (("failure", "FAILED"), ("error", "FAILED"), ("skipped", "SKIPPED")):
        el = tc.find(tag)
        if el is not None:
            return status, (el.get("message") or el.text or "").strip()[:500]
    return "PASSED", ""


def parse_python(path: Path) -> list[Case]:
    cases: list[Case] = []
    for tc in ET.parse(path).getroot().iter("testcase"):  # noqa: S314 — файл создан этим прогоном
        props = {p.get("name"): p.get("value") for p in tc.iter("property")}
        nodeid = props.get("nodeid")
        if not nodeid:
            file = tc.get("file") or tc.get("classname", "").replace(".", "/") + ".py"
            nodeid = f"{file}::{tc.get('name')}"
        status, message = _status(tc)
        cases.append(
            Case(
                "python", nodeid, nodeid.split("::")[0], tc.get("name", ""), status, float(tc.get("time") or 0), message
            )
        )
    return cases


def parse_web(path: Path, suite: str) -> list[Case]:
    cases: list[Case] = []
    for tc in ET.parse(path).getroot().iter("testcase"):  # noqa: S314 — файл создан этим прогоном
        file, title = tc.get("classname", ""), tc.get("name", "")
        status, message = _status(tc)
        cases.append(Case(suite, f"{file}::{title}", file, title, status, float(tc.get("time") or 0), message))
    return cases


# ---------------------------------------------------------------------------------------------- отчёт


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False).stdout.strip()  # noqa: S603, S607


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate(m: acceptance_matrix.Matrix, cases: dict[str, list[Case]], ran: set[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for c in m.criteria:
        clauses: list[dict[str, Any]] = []
        for cl in c.clauses:
            if cl.kind == "real_cli":
                clauses.append(
                    {
                        "text": cl.text,
                        "kind": "real_cli",
                        "status": "BLOCKED",
                        "reason": m.real_cli_blocked_reason,
                        "tests": [],
                    }
                )
                continue
            tests: list[dict[str, Any]] = []
            for ref in cl.tests:
                suite = ref.suite
                if suite not in ran:
                    tests.append({"ref": ref.ref, "suite": suite, "status": "NOT_RUN", "cases": []})
                    continue
                if ref.kind == "py":
                    found = [x for x in cases.get("python", []) if ref.matches_py(x.key)]
                else:
                    found = [x for x in cases.get(suite, []) if ref.matches_web(suite, x.file, x.title)]
                if not found:
                    status = "MISSING"
                elif any(x.status == "FAILED" for x in found):
                    status = "FAILED"
                elif any(x.status == "SKIPPED" for x in found):
                    status = "SKIPPED"
                else:
                    status = "PASSED"
                tests.append(
                    {
                        "ref": ref.ref,
                        "suite": suite,
                        "status": status,
                        "cases": [
                            {
                                "id": x.key,
                                "status": x.status,
                                "time": round(x.time, 2),
                                **({"message": x.message} if x.message else {}),
                            }
                            for x in found
                        ],
                    }
                )
            statuses = {t["status"] for t in tests}
            if statuses & {"FAILED", "MISSING"}:
                status = "FAIL"
            elif statuses & {"SKIPPED", "NOT_RUN"}:
                status = "INCOMPLETE"
            else:
                status = "PASS"
            clauses.append({"text": cl.text, "kind": "fake", "status": status, "tests": tests})
        fake = [x["status"] for x in clauses if x["kind"] == "fake"]
        status = "FAIL" if "FAIL" in fake else "INCOMPLETE" if "INCOMPLETE" in fake else "PASS"
        rc = [x for x in clauses if x["kind"] == "real_cli"]
        result.append(
            {
                "id": c.id,
                "title": c.title,
                "status": status,
                "real_cli": "BLOCKED" if rc else "NOT_REQUIRED",
                "clauses": clauses,
            }
        )
    return result


def _counts(items: list[Case]) -> dict[str, int]:
    out = {"total": len(items), "passed": 0, "failed": 0, "skipped": 0}
    for x in items:
        out[x.status.lower()] += 1
    return out


def write_report(
    out: Path,
    runs: dict[str, dict[str, Any]],
    cases: dict[str, list[Case]],
    acs: list[dict[str, Any]],
    m: acceptance_matrix.Matrix,
) -> dict[str, Any]:
    dirty = [line for line in _git("status", "--porcelain").splitlines() if line]
    diff = subprocess.run(["git", "-C", str(ROOT), "diff", "HEAD"], capture_output=True, check=False).stdout  # noqa: S603, S607
    passed = sum(1 for a in acs if a["status"] == "PASS")
    doc: dict[str, Any] = {
        "report": "AiCodingDev acceptance AC-01…AC-16 (P9-01)",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "commit_sha": _git("rev-parse", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "working_tree": {
            "dirty": bool(dirty),
            "changed_paths": len(dirty),
            "diff_vs_head_sha256": hashlib.sha256(diff).hexdigest() if dirty else None,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "fake_cli": True,
            "real_cli": "BLOCKED",
        },
        "matrix": str(acceptance_matrix.MATRIX_PATH.relative_to(ROOT)),
        "matrix_sha256": _sha256(acceptance_matrix.MATRIX_PATH),
        "runs": runs,
        "junit": {
            s: {"path": str(p.relative_to(ROOT)), "sha256": _sha256(p)}
            for s in SUITES
            if (p := out / "junit" / f"{'python' if s == 'python' else 'web-' + s}.xml").exists()
        },
        "counts": {s: _counts(cases.get(s, [])) for s in SUITES if s in cases},
        "summary": {
            "criteria": len(acs),
            "pass": passed,
            "fail": sum(a["status"] == "FAIL" for a in acs),
            "incomplete": sum(a["status"] == "INCOMPLETE" for a in acs),
            "real_cli_blocked": sum(a["real_cli"] == "BLOCKED" for a in acs),
            "all_mandatory_pass": passed == len(acs),
        },
        "real_cli_blocked_reason": m.real_cli_blocked_reason,
        "criteria": acs,
    }
    (out / "acceptance.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "ACCEPTANCE_REPORT.md").write_text(render_md(doc), encoding="utf-8")
    return doc


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_md(doc: dict[str, Any]) -> str:
    s = doc["summary"]
    wt = doc["working_tree"]
    lines = [
        "# Acceptance report AC-01…AC-16 (P9-01)",
        "",
        "> Сгенерировано `scripts/run_acceptance.py` из junit-отчётов прогона — не редактировать вручную.",
        "",
        f"- **Дата (UTC):** {doc['generated_at']}",
        f"- **Commit:** `{doc['commit_sha']}` (ветка `{doc['branch']}`)",
        "- **Рабочее дерево:** "
        + (
            f"изменено ({wt['changed_paths']} путей; sha256 `git diff HEAD` = `{wt['diff_vs_head_sha256']}`)"
            if wt["dirty"]
            else "чистое"
        ),
        f"- **Окружение:** Python {doc['environment']['python']}, {doc['environment']['platform']}; "
        "исполнитель агентов — Fake CLI; реальные CLI — BLOCKED",
        f"- **Матрица:** `{doc['matrix']}` (sha256 `{doc['matrix_sha256']}`)",
        f"- **Итог:** {s['pass']}/{s['criteria']} AC PASS (Fake), FAIL {s['fail']}, INCOMPLETE {s['incomplete']}; "
        f"real-CLI часть BLOCKED у {s['real_cli_blocked']} AC. "
        + (
            "**100% обязательных acceptance-тестов (Fake) проходят.**"
            if s["all_mandatory_pass"]
            else "**Критерий «100% обязательных acceptance-тестов» НЕ выполнен.**"
        ),
        "",
        "## Прогоны",
        "",
        "| Набор | Команда | Каталог | Exit | Начало | Длительность, с | Тесты (passed/failed/skipped) | junit sha256 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for suite in SUITES:
        run = doc["runs"].get(suite)
        if run is None:
            continue
        c = doc["counts"].get(suite, {"passed": 0, "failed": 0, "skipped": 0, "total": 0})
        junit = doc["junit"].get(suite, {}).get("sha256", "—")
        lines.append(
            f"| {suite} | `{_cell(run.get('command', '—'))}` | `{run.get('cwd', '—')}` | {run.get('exit_code', '—')} | "
            f"{run.get('started_at', '—')} | {run.get('duration_seconds', '—')} | "
            f"{c['passed']}/{c['failed']}/{c['skipped']} из {c['total']} | `{junit[:16]}…` |"
        )
    lines += [
        "",
        "## Матрица AC",
        "",
        "| AC | Критерий | Fake | Real CLI | Пункты (PASS/всего) |",
        "|---|---|---|---|---|",
    ]
    for a in doc["criteria"]:
        fake = [c for c in a["clauses"] if c["kind"] == "fake"]
        ok = sum(c["status"] == "PASS" for c in fake)
        lines.append(f"| {a['id']} | {_cell(a['title'])} | **{a['status']}** | {a['real_cli']} | {ok}/{len(fake)} |")
    lines += ["", f"**Real CLI — BLOCKED:** {doc['real_cli_blocked_reason']}", "", "## Пункты и тесты", ""]
    for a in doc["criteria"]:
        lines += [f"### {a['id']} — {a['title']} — {a['status']}", ""]
        for i, c in enumerate(a["clauses"], 1):
            lines.append(f"{i}. **{c['status']}** — {c['text']}")
            if c["kind"] == "real_cli":
                lines.append("   - real-CLI: BLOCKED (см. причину выше)")
            for t in c["tests"]:
                n = len(t["cases"])
                extra = f" ({n} случ.)" if n > 1 else ""
                lines.append(f"   - `{t['status']}` [{t['suite']}] `{t['ref']}`{extra}")
                for case in t["cases"]:
                    if case["status"] != "PASSED" and case.get("message"):
                        lines.append(f"     - {case['status']}: `{case['id']}` — {_cell(case['message'][:300])}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--skip-web", action="store_true", help="не запускать Playwright (web-пункты NOT_RUN)")
    parser.add_argument("--skip-live", action="store_true", help="не запускать web live (стенд)")
    parser.add_argument("--skip-mocked", action="store_true", help="не запускать web mocked")
    parser.add_argument("--report-only", action="store_true", help="только пересобрать отчёт из junit в --out")
    parser.add_argument("pytest_args", nargs="*", help="дополнительные аргументы pytest (после --)")
    args = parser.parse_args(argv)
    out: Path = args.out.resolve()
    (out / "junit").mkdir(parents=True, exist_ok=True)
    (out / "logs").mkdir(parents=True, exist_ok=True)
    m = acceptance_matrix.load()

    runs_path = out / "runs.json"
    runs: dict[str, dict[str, Any]] = (
        json.loads(runs_path.read_text()) if args.report_only and runs_path.exists() else {}
    )
    if not args.report_only:
        if not os.environ.get("AICD_TEST_DATABASE_ADMIN_URL"):
            print(
                "ВНИМАНИЕ: AICD_TEST_DATABASE_ADMIN_URL не задан — тесты с БД будут SKIPPED (≠ PASS)", file=sys.stderr
            )
        for stale in (out / "junit").glob("*.xml"):
            stale.unlink()
        runs["python"] = run_python(out, args.pytest_args)
        if not (args.skip_web or args.skip_live):
            runs["live"] = run_live(out)
        if not (args.skip_web or args.skip_mocked):
            runs["mocked"] = run_mocked(out)
        runs_path.write_text(json.dumps(runs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    cases: dict[str, list[Case]] = {}
    ran: set[str] = set()
    for suite, name in (("python", "python.xml"), ("live", "web-live.xml"), ("mocked", "web-mocked.xml")):
        path = out / "junit" / name
        if suite in runs and path.exists():
            cases[suite] = parse_python(path) if suite == "python" else parse_web(path, suite)
            ran.add(suite)
        elif suite in runs:
            runs[suite]["note"] = "junit-отчёт не создан — набор считается не выполненным"
    acs = evaluate(m, cases, ran)
    doc = write_report(out, runs, cases, acs, m)
    s = doc["summary"]
    print(
        f"AC PASS {s['pass']}/{s['criteria']}, FAIL {s['fail']}, INCOMPLETE {s['incomplete']}, "
        f"real-CLI BLOCKED {s['real_cli_blocked']} → {out / 'ACCEPTANCE_REPORT.md'}"
    )
    for a in acs:
        print(f"  {a['id']}: {a['status']}" + (" (real CLI: BLOCKED)" if a["real_cli"] == "BLOCKED" else ""))
    return 0 if s["all_mandatory_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
