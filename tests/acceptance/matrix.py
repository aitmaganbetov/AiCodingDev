"""Загрузка и проверка матрицы приёмки (tests/acceptance/matrix.yaml) — общая для conftest и scripts/run_acceptance.py."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MATRIX_PATH = Path(__file__).with_name("matrix.yaml")
WEB_SUITES = ("live", "mocked")


@dataclass(frozen=True)
class TestRef:
    kind: str  # "py" | "web"
    ref: str  # node id (py) или "<suite>:<file>::<title prefix>" (web)

    @property
    def suite(self) -> str:
        return "python" if self.kind == "py" else self.ref.split(":", 1)[0]

    def web_parts(self) -> tuple[str, str, str]:
        suite, rest = self.ref.split(":", 1)
        file, title = rest.split("::", 1)
        return suite, file, title

    def matches_py(self, nodeid: str) -> bool:
        return self.kind == "py" and (nodeid == self.ref or nodeid.startswith(self.ref + "["))

    def matches_web(self, suite: str, file: str, title: str) -> bool:
        if self.kind != "web":
            return False
        s, f, t = self.web_parts()
        return s == suite and f == file and title.startswith(t)


@dataclass(frozen=True)
class Clause:
    text: str
    kind: str  # "fake" | "real_cli"
    tests: tuple[TestRef, ...]


@dataclass(frozen=True)
class Criterion:
    id: str
    title: str
    clauses: tuple[Clause, ...]


@dataclass(frozen=True)
class Matrix:
    criteria: tuple[Criterion, ...]
    real_cli_blocked_reason: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    def py_refs(self) -> dict[str, set[str]]:
        """node id префикс → множество AC, которые он доказывает."""
        out: dict[str, set[str]] = {}
        for c in self.criteria:
            for clause in c.clauses:
                for t in clause.tests:
                    if t.kind == "py":
                        out.setdefault(t.ref, set()).add(c.id)
        return out

    def acs_for_nodeid(self, nodeid: str) -> set[str]:
        found: set[str] = set()
        for ref, acs in self.py_refs().items():
            if nodeid == ref or nodeid.startswith(ref + "["):
                found |= acs
        return found


def load(path: Path = MATRIX_PATH) -> Matrix:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    criteria: list[Criterion] = []
    for c in raw["criteria"]:
        clauses: list[Clause] = []
        for cl in c["clauses"]:
            kind = cl.get("kind", "fake")
            refs: list[TestRef] = []
            for t in cl.get("tests", []):
                ((k, v),) = t.items()
                if k not in ("py", "web"):
                    raise ValueError(f"{c['id']}: неизвестный вид теста {k}")
                ref = TestRef(k, str(v))
                if k == "web":
                    suite, _, title = ref.web_parts()
                    if suite not in WEB_SUITES or not title:
                        raise ValueError(f"{c['id']}: неверная web-ссылка {v}")
                refs.append(ref)
            if kind == "fake" and not refs:
                raise ValueError(f"{c['id']}: пункт «{cl['text']}» без тестов")
            if kind not in ("fake", "real_cli"):
                raise ValueError(f"{c['id']}: неизвестный kind {kind}")
            clauses.append(Clause(str(cl["text"]), kind, tuple(refs)))
        criteria.append(Criterion(str(c["id"]), str(c["title"]), tuple(clauses)))
    ids = [c.id for c in criteria]
    expected = [f"AC-{i:02d}" for i in range(1, 17)]
    if ids != expected:
        raise ValueError(f"матрица должна содержать AC-01…AC-16 по порядку, есть {ids}")
    return Matrix(tuple(criteria), " ".join(str(raw["real_cli_blocked_reason"]).split()), raw)
