#!/usr/bin/env python3
"""Release security gate (P9-07): релиз блокируется, если в модели угроз есть открытая угроза уровня high/critical.

Источник — реестр статусов угроз `docs/security/THREAT_MODEL.md` §10 (таблица `| TH-NN | risk | residual | статус |
закрытие |`), сверяемый с таблицами угроз §5 (те же ID и уровни). Для угроз с inherent или residual риском
`high`/`critical`:

* `closed` — допустимо, только если residual не выше `medium` и среди контролей закрытия есть контроль с
  автоматическими тестами (`tests/security/catalog.py`, вид automated/partial); с `--junit` (прогон
  `pytest -m security`) — ни один тест контролей закрытия не упал и хотя бы один прошёл;
* `accepted:OQ-NN` — принятый риск, требующий решения владельца: вопрос OQ-NN обязан существовать в
  `docs/planning/OPEN_QUESTIONS.md`; пока в «Журнале решений» нет записи по OQ-NN, gate считает риск
  **ожидающим решения** и с `--require-decisions` (режим релиза) падает;
* `open` (или любой другой статус) — gate падает.

    uv run python scripts/security_gate.py [--junit reports/security/junit.xml] [--require-decisions]

Код выхода: 0 — открытых high/critical нет; 1 — есть нарушения (перечислены в выводе).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.security import catalog  # noqa: E402

THREAT_MODEL = ROOT / "docs" / "security" / "THREAT_MODEL.md"
OPEN_QUESTIONS = ROOT / "docs" / "planning" / "OPEN_QUESTIONS.md"
LEVELS = ("low", "medium", "high", "critical")
GATED = frozenset({"high", "critical"})
_SC = re.compile(r"SC-[A-Z]+-\d+")


@dataclass(frozen=True)
class Threat:
    tid: str
    risk: str
    residual: str
    status: str = ""
    closure: tuple[str, ...] = ()


def _level(text: str) -> str:
    m = re.search(r"\b(low|medium|high|critical)\b", text.replace("*", ""))
    return m.group(1) if m else ""


def parse_threat_tables(text: str) -> dict[str, Threat]:
    """§5: строки `| TH-NN | элемент | STRIDE | описание | L | I | Risk | митигации | Residual | проверка |`."""
    section = text.split("## 5.", 1)[1].split("\n## 6.", 1)[0] if "## 5." in text else ""
    threats = {}
    for line in section.splitlines():
        if not line.startswith("| TH-"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 10:
            continue
        threats[cells[0]] = Threat(cells[0], _level(cells[6]), _level(cells[8]))
    return threats


def parse_registry(text: str) -> dict[str, Threat]:
    """§10: `| TH-NN | risk | residual | статус | контроли закрытия / обоснование |`."""
    section = text.split("## 10.", 1)[1].split("\n## 11.", 1)[0] if "## 10." in text else ""
    registry = {}
    for line in section.splitlines():
        if not line.startswith("| TH-"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        status = cells[3].replace("`", "").replace("*", "").strip()
        registry[cells[0]] = Threat(cells[0], _level(cells[1]), _level(cells[2]), status, tuple(_SC.findall(cells[4])))
    return registry


def open_questions(text: str) -> set[str]:
    return set(re.findall(r"^\| (OQ-\d+) \|", text, flags=re.M))


def decided_questions(text: str) -> set[str]:
    """OQ, по которым есть запись в «Журнал решений» (колонка «Вопрос»: `OQ-NN` или диапазон `OQ-a…OQ-b`)."""
    log = text.split("Журнал решений", 1)[1] if "Журнал решений" in text else ""
    decided: set[str] = set()
    for line in log.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not re.match(r"\d{4}-\d{2}-\d{2}", cells[0]):
            continue
        for m in re.finditer(r"OQ-(\d+)(?:\s*…\s*OQ-(\d+))?", cells[1]):
            lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
            decided.update(f"OQ-{n:02d}" for n in range(lo, hi + 1))
    return decided


def junit_control_status(junit: Path) -> dict[str, str]:
    from scripts.security_report import evaluate, parse_junit

    return {r.cid: r.status for r in evaluate(parse_junit(junit))}


def check(
    threat_text: str,
    oq_text: str,
    *,
    control_status: dict[str, str] | None = None,
    require_decisions: bool = False,
) -> tuple[list[str], list[str]]:
    """(нарушения, предупреждения)."""
    threats = parse_threat_tables(threat_text)
    registry = parse_registry(threat_text)
    known = open_questions(oq_text)
    decided = decided_questions(oq_text)
    errors: list[str] = []
    warnings: list[str] = []
    if not threats:
        errors.append("в THREAT_MODEL §5 не найдены угрозы")
    for tid in sorted(set(threats) - set(registry)):
        errors.append(f"{tid}: нет в реестре статусов §10")
    for tid in sorted(set(registry) - set(threats)):
        errors.append(f"{tid}: в реестре §10, но нет в таблицах §5")
    for tid, t in sorted(threats.items()):
        r = registry.get(tid)
        if r is None:
            continue
        if (r.risk, r.residual) != (t.risk, t.residual):
            errors.append(f"{tid}: уровни в §10 ({r.risk}/{r.residual}) ≠ §5 ({t.risk}/{t.residual})")
        if t.risk not in LEVELS or t.residual not in LEVELS:
            errors.append(f"{tid}: не указан уровень риска (inherent/residual)")
            continue
        if t.risk not in GATED and t.residual not in GATED:
            continue
        status = r.status
        if status == "closed":
            if t.residual in GATED:
                errors.append(f"{tid}: residual {t.residual} — угроза не может быть closed (нужно accepted:OQ-NN)")
            automated = [c for c in r.closure if c in catalog.CONTROLS and catalog.CONTROLS[c].tests]
            unknown = [c for c in r.closure if c not in catalog.CONTROLS]
            if unknown:
                errors.append(f"{tid}: неизвестные контроли закрытия {unknown}")
            if not automated:
                errors.append(f"{tid}: closed без контроля с автоматическими тестами")
            elif control_status is not None:
                failed = [c for c in automated if control_status.get(c) == "FAIL"]
                passed = [c for c in automated if control_status.get(c) in ("PASS", "PARTIAL-SKIP")]
                if failed:
                    errors.append(f"{tid}: тесты контролей закрытия упали: {failed}")
                elif not passed:
                    errors.append(f"{tid}: ни один контроль закрытия не подтверждён прогоном ({automated})")
        elif status.startswith("accepted:"):
            oq = status.split(":", 1)[1].strip()
            if oq not in known:
                errors.append(f"{tid}: принятый риск ссылается на несуществующий {oq}")
            elif oq not in decided:
                msg = f"{tid}: принятый риск {t.risk}/{t.residual} ожидает решения владельца ({oq})"
                (errors if require_decisions else warnings).append(msg)
        else:
            errors.append(f"{tid}: открытая угроза {t.risk}/{t.residual} (статус «{status or '—'}»)")
    return errors, warnings


def _print(lines: Iterable[str], prefix: str) -> None:
    for line in lines:
        print(f"{prefix} {line}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threat-model", type=Path, default=THREAT_MODEL)
    parser.add_argument("--open-questions", type=Path, default=OPEN_QUESTIONS)
    parser.add_argument("--junit", type=Path, help="junit-XML прогона `pytest -m security`")
    parser.add_argument(
        "--require-decisions", action="store_true", help="принятые риски без решения владельца — ошибка"
    )
    args = parser.parse_args(argv)
    status = junit_control_status(args.junit) if args.junit else None
    errors, warnings = check(
        args.threat_model.read_text(encoding="utf-8"),
        args.open_questions.read_text(encoding="utf-8"),
        control_status=status,
        require_decisions=args.require_decisions,
    )
    threats = parse_threat_tables(args.threat_model.read_text(encoding="utf-8"))
    gated = sum(1 for t in threats.values() if t.risk in GATED or t.residual in GATED)
    _print(warnings, "WARN")
    _print(errors, "FAIL")
    print(
        f"security gate: угроз {len(threats)}, high/critical {gated}, нарушений {len(errors)}, ожидают решения {len(warnings)}"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
