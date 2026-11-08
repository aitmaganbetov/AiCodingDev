"""OQ-49: каталог outbox-событий EVENT_SCHEMAS §2.2 совпадает с тем, что код записывает в `outbox_events`.

Источники типов в коде:
* вызовы `aicd_platform.outbox.emit(...)` в `packages/**` и `apps/**` (AST): `event_type`/`aggregate_type` —
  строковый литерал или константа модуля (своя или импортированная); иначе тест падает — тип должен быть
  статически известен, чтобы каталог можно было сверить;
* SQL-функции миграций с `INSERT INTO outbox_events (…) VALUES (…)` (литералы в позиции `event_type`).
Каталог — строки таблицы раздела `### 2.2 Каталог` (первая ячейка — `event_type`, вторая — агрегат).
"""

from __future__ import annotations

import ast
import importlib
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVENT_SCHEMAS = ROOT / "docs" / "protocols" / "EVENT_SCHEMAS.md"
MIGRATIONS = ROOT / "infrastructure" / "migrations" / "versions"
# CHECK ck_outbox_events_event_type (миграция 0003) / outbox-envelope.schema.json.
EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")

Emitted = dict[str, set[tuple[str, str]]]  # event_type → {(aggregate_type, где)}


def _python_sources() -> list[Path]:
    return sorted({*ROOT.glob("packages/*/aicd_*/**/*.py"), *ROOT.glob("apps/*/aicd_*/**/*.py")})


class _Resolver:
    """Значение выражения-аргумента: литерал, константа модуля, импортированная константа или `mod.CONST`."""

    def __init__(self, tree: ast.Module) -> None:
        self.constants: dict[str, str] = {}
        self.imported: dict[str, tuple[str, str]] = {}  # локальное имя → (модуль, атрибут)
        self.modules: dict[str, str] = {}  # локальное имя → модуль
        for node in tree.body:
            if isinstance(node, ast.Assign | ast.AnnAssign) and isinstance(node.value, ast.Constant):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and isinstance(node.value.value, str):
                        self.constants[target.id] = node.value.value
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                for alias in node.names:
                    self.imported[alias.asname or alias.name] = (node.module, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.modules[alias.asname or alias.name.split(".")[0]] = alias.name

    def resolve(self, expr: ast.expr) -> str | None:
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return expr.value
        if isinstance(expr, ast.Name):
            if expr.id in self.constants:
                return self.constants[expr.id]
            if expr.id in self.imported:
                module, attr = self.imported[expr.id]
                value = getattr(importlib.import_module(module), attr, None)
                return value if isinstance(value, str) else None
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            module = self.modules.get(expr.value.id) or ".".join(self.imported.get(expr.value.id, ("", "")))
            if module:
                value = getattr(importlib.import_module(module), expr.attr, None)
                return value if isinstance(value, str) else None
        return None


def _is_outbox_emit(call: ast.Call, resolver: _Resolver) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "emit" and isinstance(func.value, ast.Name):
        return resolver.imported.get(func.value.id) == ("aicd_platform", "outbox") or (
            resolver.modules.get(func.value.id) == "aicd_platform.outbox"
        )
    return isinstance(func, ast.Name) and resolver.imported.get(func.id) == ("aicd_platform.outbox", "emit")


def emitted_by_python() -> tuple[Emitted, list[str]]:
    found: Emitted = defaultdict(set)
    unresolved: list[str] = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        resolver = _Resolver(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_outbox_emit(node, resolver)):
                continue
            where = f"{path.relative_to(ROOT)}:{node.lineno}"
            kwargs = {k.arg: k.value for k in node.keywords if k.arg}
            event_type = resolver.resolve(kwargs["event_type"]) if "event_type" in kwargs else None
            aggregate = resolver.resolve(kwargs["aggregate_type"]) if "aggregate_type" in kwargs else None
            if event_type is None or aggregate is None:
                unresolved.append(where)
                continue
            found[event_type].add((aggregate, where))
    return found, unresolved


def _split_top_level(sql: str) -> tuple[list[str], int]:
    """Элементы списка `(a, f(b, c), 'x,y')` до закрывающей скобки и позиция за ней (`sql` — после `(`)."""
    items, depth, start, i, quoted = [], 0, 0, 0, False
    while i < len(sql):
        ch = sql[i]
        if quoted:
            if ch == "'" and sql[i + 1 : i + 2] == "'":
                i += 1
            elif ch == "'":
                quoted = False
        elif ch == "'":
            quoted = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                items.append(sql[start:i].strip())
                return items, i + 1
            depth -= 1
        elif ch == "," and depth == 0:
            items.append(sql[start:i].strip())
            start = i + 1
        i += 1
    raise AssertionError("незакрытый список VALUES")


_SQL_INSERT = re.compile(r"INSERT\s+INTO\s+(?:aicd\.)?outbox_events\s*\(([^)]*)\)\s*VALUES\s*\(", re.I)


def emitted_by_sql() -> tuple[Emitted, list[str]]:
    found: Emitted = defaultdict(set)
    unresolved: list[str] = []
    for path in sorted(MIGRATIONS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in _SQL_INSERT.finditer(text):
            columns = [c.strip() for c in match.group(1).split(",")]
            values, _ = _split_top_level(text[match.end() :])
            where = f"{path.relative_to(ROOT)}:{text.count(chr(10), 0, match.start()) + 1}"
            row = dict(zip(columns, values, strict=True))
            literal = re.compile(r"^'([^']*)'$")
            et, agg = literal.match(row["event_type"]), literal.match(row["aggregate_type"])
            if et is None or agg is None:
                unresolved.append(where)
                continue
            found[et.group(1)].add((agg.group(1), where))
    return found, unresolved


def emitted() -> Emitted:
    result: Emitted = defaultdict(set)
    for source in (emitted_by_python, emitted_by_sql):
        found, unresolved = source()
        assert not unresolved, f"event_type/aggregate_type не определяются статически: {unresolved}"
        for event_type, places in found.items():
            result[event_type] |= places
    return result


def catalog() -> dict[str, str]:
    """`event_type` → агрегат из таблицы §2.2."""
    text = EVENT_SCHEMAS.read_text(encoding="utf-8")
    section = text.split("### 2.2 Каталог", 1)[1].split("\n### ", 1)[0].split("\n## ", 1)[0]
    rows: dict[str, str] = {}
    body = section.split("\n|---", 1)[1].split("\n", 1)[1]  # строки после разделителя заголовка таблицы
    for line in body.splitlines():
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        name = re.match(r"^`([^`]+)`", cells[0])
        assert name is not None, line
        assert name.group(1) not in rows, f"дубль строки каталога: {name.group(1)}"
        rows[name.group(1)] = cells[1]
    return rows


def test_collector_sees_python_and_sql_emitters() -> None:
    python, _ = emitted_by_python()
    sql, _ = emitted_by_sql()
    assert len(python) >= 15 and {"run.created", "gate.completed", "chat.message"} <= set(python)
    assert {"github.webhook.received", "approval.requested"} <= set(sql)


def test_every_emitted_event_type_is_in_catalog() -> None:
    code, docs = emitted(), catalog()
    missing = {et: sorted(w for _, w in places) for et, places in code.items() if et not in docs}
    assert not missing, f"события из кода без строки в EVENT_SCHEMAS §2.2: {missing}"


def test_every_catalog_event_type_is_emitted() -> None:
    code, docs = emitted(), catalog()
    phantom = sorted(set(docs) - set(code))
    assert not phantom, f"строки EVENT_SCHEMAS §2.2 без производителя в коде (перенесите в §2.2a): {phantom}"


def test_catalog_aggregates_and_names_match_code() -> None:
    code, docs = emitted(), catalog()
    for event_type, aggregate in docs.items():
        assert EVENT_TYPE_RE.fullmatch(event_type), event_type
        if event_type in code:
            assert {aggregate} == {a for a, _ in code[event_type]}, (event_type, aggregate, code[event_type])
