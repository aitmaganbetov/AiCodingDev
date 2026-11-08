"""Валидация DAG плана (DATA_FLOW §4.1, P3-05): ацикличность (алгоритм Кана), лимиты, роли.

Чистая функция над вершинами/рёбрами; транзакционная часть (блокировка run, plan_version,
контрольная проверка триггером БД) — в `plans`. Ошибка цикла содержит путь цикла для UI.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

AGENT_ROLES = frozenset(
    {
        "requirements",
        "architect",
        "planner",
        "developer",
        "reviewer",
        "security",
        "qa",
        "database",
        "performance",
        "devops",
        "maintenance",
        "documentation",
    }
)
VERIFYING_ROLES = frozenset({"reviewer", "qa", "security"})


@dataclass(frozen=True)
class PlannedTask:
    key: str
    role: str
    complexity: int
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanLimits:
    max_tasks: int = 50
    max_depth: int = 20


@dataclass
class PlanValidationError(Exception):
    code: str
    message: str
    details: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")


def topological_order(tasks: list[PlannedTask]) -> list[str]:
    """Порядок Кана; при цикле — PlanValidationError('PLAN_CYCLE_DETECTED') с вершинами цикла."""
    keys = {t.key for t in tasks}
    indegree: dict[str, int] = dict.fromkeys(keys, 0)
    children: dict[str, list[str]] = defaultdict(list)
    for t in tasks:
        for dep in t.depends_on:
            indegree[t.key] += 1
            children[dep].append(t.key)
    queue = deque(sorted(k for k, d in indegree.items() if d == 0))
    order: list[str] = []
    while queue:
        k = queue.popleft()
        order.append(k)
        for child in sorted(children[k]):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(order) != len(keys):
        cycle = _find_cycle({t.key: t.depends_on for t in tasks if indegree[t.key] > 0})
        raise PlanValidationError("PLAN_CYCLE_DETECTED", "план содержит цикл зависимостей", {"cycle": cycle})
    return order


def _find_cycle(graph: dict[str, tuple[str, ...]]) -> list[str]:
    visiting: list[str] = []
    seen: set[str] = set()

    def dfs(node: str) -> list[str] | None:
        if node in visiting:
            return [*visiting[visiting.index(node) :], node]
        if node in seen or node not in graph:
            return None
        visiting.append(node)
        for nxt in graph[node]:
            found = dfs(nxt)
            if found:
                return found
        visiting.pop()
        seen.add(node)
        return None

    for start in sorted(graph):
        found = dfs(start)
        if found:
            return found
    return sorted(graph)  # pragma: no cover — цикл гарантирован вызывающим


def validate_plan(tasks: list[PlannedTask], limits: PlanLimits | None = None) -> list[str]:
    """Полная проверка плана; возвращает топологический порядок ключей."""
    limits = limits or PlanLimits()
    if not tasks:
        raise PlanValidationError("PLAN_EMPTY", "план не содержит задач")
    if len(tasks) > limits.max_tasks:
        raise PlanValidationError("PLAN_TOO_LARGE", f"задач больше {limits.max_tasks}", {"count": len(tasks)})
    keys = [t.key for t in tasks]
    if len(set(keys)) != len(keys):
        raise PlanValidationError("PLAN_DUPLICATE_KEY", "ключи задач повторяются")
    known = set(keys)
    for t in tasks:
        if t.role not in AGENT_ROLES:
            raise PlanValidationError("PLAN_UNKNOWN_ROLE", f"неизвестная роль {t.role}", {"task": t.key})
        if not 1 <= t.complexity <= 5:
            raise PlanValidationError("PLAN_BAD_COMPLEXITY", "сложность вне 1..5", {"task": t.key})
        if t.key in t.depends_on:
            raise PlanValidationError("PLAN_CYCLE_DETECTED", "задача зависит от самой себя", {"cycle": [t.key, t.key]})
        missing = [d for d in t.depends_on if d not in known]
        if missing:
            raise PlanValidationError(
                "PLAN_UNKNOWN_DEPENDENCY", "зависимость от несуществующей задачи", {"task": t.key, "missing": missing}
            )
    order = topological_order(tasks)
    depth = _depths(tasks, order)
    if max(depth.values()) > limits.max_depth:
        raise PlanValidationError("PLAN_TOO_DEEP", f"глубина больше {limits.max_depth}")
    _require_verification_for_l3(tasks)
    return order


def _depths(tasks: list[PlannedTask], order: list[str]) -> dict[str, int]:
    by_key = {t.key: t for t in tasks}
    depth: dict[str, int] = {}
    for k in order:
        depth[k] = 1 + max((depth[d] for d in by_key[k].depends_on), default=0)
    return depth


def _require_verification_for_l3(tasks: list[PlannedTask]) -> None:
    """У developer-задач L3+ должен быть зависимый верифицирующий узел (reviewer/qa/security)."""
    dependents: dict[str, list[PlannedTask]] = defaultdict(list)
    for t in tasks:
        for d in t.depends_on:
            dependents[d].append(t)
    for t in tasks:
        if t.role == "developer" and t.complexity >= 3 and not _reaches_verifier(t.key, dependents):
            raise PlanValidationError(
                "PLAN_MISSING_VERIFICATION", "задача developer уровня L3+ без последующей проверки", {"task": t.key}
            )


def _reaches_verifier(key: str, dependents: dict[str, list[PlannedTask]]) -> bool:
    stack, seen = [key], set()
    while stack:
        cur = stack.pop()
        for child in dependents.get(cur, []):
            if child.role in VERIFYING_ROLES:
                return True
            if child.key not in seen:
                seen.add(child.key)
                stack.append(child.key)
    return False


def critical_path_lengths(tasks: list[PlannedTask]) -> dict[str, int]:
    """Длина самой длинной цепочки зависимых от задачи (приоритет планировщика, DATA_FLOW §4.2)."""
    dependents: dict[str, list[str]] = defaultdict(list)
    for t in tasks:
        for d in t.depends_on:
            dependents[d].append(t.key)
    order = topological_order(tasks)
    length: dict[str, int] = {}
    for k in reversed(order):
        length[k] = 1 + max((length[c] for c in dependents[k]), default=0)
    return length
