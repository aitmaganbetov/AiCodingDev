#!/usr/bin/env python3
"""Отчёт security suite (P9-02): контроль SECURITY_CONTROLS → тесты → результат фактического прогона.

Источник результатов — junit-XML прогона `pytest -m security` (только он: отчёт не «считает» тесты, которые
не выполнялись), источник сопоставления — `tests/security/catalog.py`.

    uv run pytest -m security --junitxml=reports/security/junit.xml
    uv run python scripts/security_report.py --junit reports/security/junit.xml \
        --out reports/security/SECURITY_REPORT.md [--strict]

Статус контроля: PASS — все сопоставленные тесты прошли; FAIL — хотя бы один упал/ошибка; SKIP — все
пропущены (нет Docker и т. п.); PARTIAL-SKIP — часть прошла, часть пропущена; NOT RUN — ни одного
сопоставленного теста нет в junit; MANUAL — автоматического теста нет (организационный контроль).
`--strict`: код выхода 1, если есть FAIL или NOT RUN у автоматизированных контролей.
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.security import catalog  # noqa: E402

CONTROLS_DOC = ROOT / "docs" / "security" / "SECURITY_CONTROLS.md"


@dataclass
class Case:
    nodeid: str
    outcome: str  # passed | failed | error | skipped
    seconds: float
    message: str = ""


@dataclass
class ControlResult:
    cid: str
    kind: str
    status: str
    counts: Counter[str] = field(default_factory=Counter)
    cases: list[Case] = field(default_factory=list)


def nodeid_of(classname: str, name: str) -> str:
    """`tests.security.test_x` + `test_y[p]` → `tests/security/test_x.py::test_y[p]` (классы тестов не используются)."""
    return classname.replace(".", "/") + ".py::" + name


def parse_junit(path: Path) -> list[Case]:
    tree = ET.parse(path)  # noqa: S314 — файл собственного прогона
    cases = []
    for tc in tree.iter("testcase"):
        outcome = "passed"
        message = ""
        for tag in ("failure", "error", "skipped"):
            el = tc.find(tag)
            if el is not None:
                outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                message = (el.get("message") or "").strip().splitlines()[0][:200] if el.get("message") else ""
                break
        cases.append(Case(nodeid_of(tc.get("classname", ""), tc.get("name", "")), outcome, float(tc.get("time", 0))))
        cases[-1].message = message
    return cases


def control_titles() -> dict[str, str]:
    titles = {}
    for line in CONTROLS_DOC.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\| (SC-[A-Z]+-\d+) \| (.+?) \|", line)
        if m:
            text = re.sub(r"`", "", m.group(2))
            titles[m.group(1)] = text[:110] + ("…" if len(text) > 110 else "")
    return titles


def evaluate(cases: list[Case]) -> list[ControlResult]:
    results = []
    for cid, control in catalog.CONTROLS.items():
        matched = [c for c in cases if any(catalog._matches(c.nodeid, p) for p in control.tests)]
        counts: Counter[str] = Counter(c.outcome for c in matched)
        if control.kind == "manual":
            status = "MANUAL"
        elif not matched:
            status = "NOT RUN"
        elif counts["failed"] or counts["error"]:
            status = "FAIL"
        elif counts["passed"] and counts["skipped"]:
            status = "PARTIAL-SKIP"
        elif counts["skipped"]:
            status = "SKIP"
        else:
            status = "PASS"
        results.append(ControlResult(cid, control.kind, status, counts, matched))
    return results


def _md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render(cases: list[Case], results: list[ControlResult], junit: Path, command: str) -> str:
    totals = Counter(c.outcome for c in cases)
    statuses = Counter(r.status for r in results)
    titles = control_titles()
    lines = [
        "# SECURITY_REPORT — security suite AiCodingDev (P9-02)",
        "",
        f"**Сгенерировано:** {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} скриптом `scripts/security_report.py` из "
        f"junit-XML фактического прогона (`{junit.relative_to(ROOT) if junit.is_relative_to(ROOT) else junit}`). "
        "Не редактировать вручную.",
        f"**Команда прогона:** `{command}`",
        "**Сопоставление:** `tests/security/catalog.py` (контроль → тесты); описание контролей — "
        "[SECURITY_CONTROLS.md](../../docs/security/SECURITY_CONTROLS.md), угрозы — "
        "[THREAT_MODEL.md](../../docs/security/THREAT_MODEL.md).",
        "",
        "## 1. Итоги прогона",
        "",
        "| Тестов | passed | failed | error | skipped | Время, с |",
        "|---|---|---|---|---|---|",
        f"| {len(cases)} | {totals['passed']} | {totals['failed']} | {totals['error']} | {totals['skipped']} | "
        f"{sum(c.seconds for c in cases):.0f} |",
        "",
        "| Статус контроля | Количество |",
        "|---|---|",
        *[
            f"| {s} | {statuses[s]} |"
            for s in ("PASS", "PARTIAL-SKIP", "SKIP", "FAIL", "NOT RUN", "MANUAL")
            if statuses[s]
        ],
        f"| **Всего контролей** | **{len(results)}** |",
        "",
        "Статусы: PASS — все сопоставленные тесты прошли; PARTIAL-SKIP — часть пропущена (например, нет Docker); "
        "SKIP — все пропущены; FAIL — есть упавшие; NOT RUN — тесты не выполнялись в этом прогоне; MANUAL — "
        "автоматического теста нет (организационный/инфраструктурный контроль, см. примечание). Вид `partial` — "
        "тесты покрывают часть контроля, остаток — в примечании.",
        "",
        "## 2. Категории ТЗ §20 «Security»",
        "",
        "| Категория | Контроли | Тестов | Результат |",
        "|---|---|---|---|",
    ]
    by_id = {r.cid: r for r in results}
    for category in catalog.TZ20_CATEGORIES:
        cids = [cid for cid, c in catalog.CONTROLS.items() if category in c.tz20]
        rs = [by_id[c] for c in cids]
        n = len({case.nodeid for r in rs for case in r.cases})
        failed = [r.cid for r in rs if r.status == "FAIL"]
        verdict = "FAIL (" + ", ".join(failed) + ")" if failed else ("PASS" if n else "NOT RUN")
        lines.append(f"| {category} | {', '.join(cids)} | {n} | {verdict} |")
    lines += [
        "",
        "## 3. Контроль → тесты → результат",
        "",
        "| Контроль | Содержание | Вид | Тестов (pass/fail/skip) | Результат | Примечание |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        control = catalog.CONTROLS[r.cid]
        c = r.counts
        tally = f"{len(r.cases)} ({c['passed']}/{c['failed'] + c['error']}/{c['skipped']})" if r.cases else "0"
        lines.append(
            f"| {r.cid} | {_md(titles.get(r.cid, ''))} | {r.kind} | {tally} | **{r.status}** | {_md(control.note)} |"
        )
    lines += ["", "## 4. Тесты по контролям", ""]
    for r in results:
        if not r.cases:
            continue
        lines.append(f"<details><summary>{r.cid} — {r.status} ({len(r.cases)})</summary>")
        lines.append("")
        for case in sorted(r.cases, key=lambda x: x.nodeid):
            suffix = f" — {_md(case.message)}" if case.message and case.outcome != "passed" else ""
            lines.append(f"- `{case.nodeid}` — {case.outcome}{suffix}")
        lines += ["", "</details>", ""]
    problems = [c for c in cases if c.outcome in ("failed", "error")]
    lines += ["## 5. Упавшие тесты", ""]
    if problems:
        lines += [f"- `{c.nodeid}` — {c.outcome}: {_md(c.message)}" for c in problems]
    else:
        lines.append("Нет.")
    skipped = Counter(c.message or "без причины" for c in cases if c.outcome == "skipped")
    lines += ["", "## 6. Пропуски (по причине)", ""]
    lines += [f"- {n} × {_md(reason)}" for reason, n in skipped.most_common()] or ["Нет."]
    unmapped = sorted(
        c.nodeid for c in cases if c.nodeid.startswith("tests/security/") and not catalog.controls_for_nodeid(c.nodeid)
    )
    lines += ["", "## 7. Тесты tests/security без сопоставленного контроля", ""]
    lines += [f"- `{n}`" for n in unmapped] or ["Нет."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "reports" / "security" / "SECURITY_REPORT.md")
    parser.add_argument("--command", default="uv run pytest -m security --junitxml=reports/security/junit.xml")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    cases = parse_junit(args.junit)
    if not cases:
        print("junit не содержит тестов", file=sys.stderr)
        return 2
    results = evaluate(cases)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(cases, results, args.junit.resolve(), args.command), encoding="utf-8")
    statuses = Counter(r.status for r in results)
    print(f"{args.out}: {len(cases)} тестов, контроли: {dict(statuses)}")
    if args.strict and (statuses["FAIL"] or statuses["NOT RUN"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
