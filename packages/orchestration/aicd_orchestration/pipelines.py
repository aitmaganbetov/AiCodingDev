"""Шаблоны конвейеров 11 режимов run для планировщика (P4-09; AGENT_ROLES §4, ROUTING_POLICY §8).

Конвейер режима — данные (`PIPELINES`): шаги с ролью, зависимостями и условиями включения (минимальный
уровень, минимальный риск, сигнал). `build_pipeline` применяет к нему надстройки уровня L1–L5:

* L3+ — независимый reviewer после изменений кода (шаги `review` с `min_level=3`);
* L4+ — двойной анализ до реализации (две analysis-задачи профильной роли, провайдеры назначит Router
  с требованием различия) и сведение `planner`; задачи `security` и `qa` обязательны; approvals
  `PLAN_APPROVAL` (план и сведение анализа) и `MERGE` (обязателен при риске ≥ high);
* L5 — совместный архитектурный анализ (`architect` ×2 + сведение), ADR, прототип в разрешённых путях
  архитектора, архитектурное ревью прототипа, затем реализация по шаблону L4; approvals: план с ADR,
  результат прототипа, результат перед `MERGE`.

Задача `planner` фазы DISCOVERY (AGENT_ROLES §4 «planner →») в DAG исполнения не входит: шаблон и есть
детерминированный план DISCOVERY (для L1 — окончательный, ROUTING_POLICY §8; для L2+ — каркас, который
уточняет роль planner). Условные шаги с сигналами, недоступными до индексации (`needs_database`,
`public_api_change`, …), включаются только при явно переданном сигнале.

Ключи задач стабильны: `main` — основная задача режима, `review` — независимое ревью.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Final, Literal

from aicd_orchestration.classification import MODE_MINIMUM
from aicd_orchestration.dag import PlannedTask, validate_plan

RISK_ORDER: Final = ("low", "medium", "high", "critical")
_VERIFIER_ROLES: Final = frozenset({"reviewer", "qa", "security"})
SIGNALS: Final = frozenset(
    {
        "criteria_missing",  # критерии приёмки не заданы пользователем
        "needs_database",  # затронуты схема/миграции/SQL
        "public_api_change",  # изменён публичный интерфейс
        "needs_test_hooks",  # тестам нужны изменения production-кода
        "ci_change",  # затронуты CI/IaC
        "adr_needed",  # документирование архитектурного решения
        "tests_review",  # ревью включает анализ тестов
    }
)
ApprovalAction = Literal["PLAN_APPROVAL", "MANUAL_REVIEW", "MERGE"]


@dataclass(frozen=True)
class Step:
    key: str
    role: str
    title: str
    instruction: str
    after: tuple[str, ...] = ()
    min_level: int = 1
    min_risk: str | None = None
    signal: str | None = None
    writes_code: bool = False
    discovery: bool = False  # шаг до реализации (требования, архитектура): анализы L4/L5 идут после него
    criteria: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModePipeline:
    mode: str
    modifies_code: bool
    analysis_role: str  # профильная роль двойного анализа L4
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class ApprovalPoint:
    action_type: ApprovalAction
    before: tuple[str, ...]  # задачи, которые не стартуют до решения; () — до начала исполнения плана
    after: str | None  # задача, по результатам которой принимается решение; None — по плану
    required: bool
    reason: str


@dataclass(frozen=True)
class PipelineTask:
    key: str
    role: str
    title: str
    instructions: str
    depends_on: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    writes_code: bool


@dataclass(frozen=True)
class PipelinePlan:
    mode: str
    level: int
    risk_level: str
    modifies_code: bool
    tasks: tuple[PipelineTask, ...]
    approvals: tuple[ApprovalPoint, ...]
    skipped: tuple[str, ...] = field(default=())  # условные шаги, не включённые (ключ: причина)

    @property
    def requires_plan_approval(self) -> bool:
        return any(a.action_type == "PLAN_APPROVAL" and a.required for a in self.approvals)


_C_CHANGE = ("Изменение выполнено в worktree задачи", "Обязательные проверки качества пройдены")
_C_REVIEW = ("Найденные проблемы зафиксированы как findings", "Вердикт ревью вынесен")
_C_REPORT = ("Отчёт сохранён как артефакт с evidence",)


def _review(after: tuple[str, ...], *, min_level: int = 3) -> Step:
    return Step(
        "review",
        "reviewer",
        "Независимое ревью изменения",
        "Проверить изменения задач на корректность, безопасность и соответствие критериям.",
        after=after,
        min_level=min_level,
        criteria=_C_REVIEW,
    )


PIPELINES: Final[Mapping[str, ModePipeline]] = {
    p.mode: p
    for p in (
        ModePipeline(
            "CREATE",
            True,
            "architect",
            (
                Step(
                    "requirements",
                    "requirements",
                    "Требования и критерии приёмки",
                    "Сформулировать требования REQ-n с критериями Given/When/Then и трассировкой.",
                    discovery=True,
                    criteria=("Требования проверяемы и трассируемы",),
                ),
                Step(
                    "architecture",
                    "architect",
                    "Архитектура и ADR",
                    "Предложить архитектуру, черновики ADR и constraints для реализации.",
                    after=("requirements",),
                    discovery=True,
                    criteria=("Черновики ADR и constraints подготовлены",),
                ),
                Step(
                    "database",
                    "database",
                    "Схема данных и миграции",
                    "Спроектировать схему и миграции с планом отката.",
                    after=("architecture",),
                    signal="needs_database",
                    writes_code=True,
                    criteria=_C_CHANGE,
                ),
                Step(
                    "main",
                    "developer",
                    "Реализация",
                    "Реализовать решение с тестами согласно требованиям и constraints.",
                    after=("architecture", "database"),
                    writes_code=True,
                    criteria=_C_CHANGE,
                ),
                Step(
                    "test_plan",
                    "qa",
                    "Тест-план и тесты",
                    "Составить тест-план по критериям и добавить недостающие тесты.",
                    after=("architecture",),
                    writes_code=True,
                    criteria=("Каждый критерий покрыт сценарием теста",),
                ),
                Step(
                    "review",
                    "reviewer",
                    "Независимое ревью изменения",
                    "Проверить реализацию и тесты на корректность и соответствие критериям.",
                    after=("main", "test_plan"),
                    criteria=_C_REVIEW,
                ),
                Step(
                    "security",
                    "security",
                    "Threat model и security review",
                    "Построить threat model и проверить изменение на уязвимости.",
                    after=("main",),
                    criteria=_C_REVIEW,
                ),
                Step(
                    "pipeline",
                    "devops",
                    "CI/CD и сборка",
                    "Настроить конвейер сборки и проверок проекта.",
                    after=("review", "security"),
                    writes_code=True,
                    criteria=_C_CHANGE,
                ),
                Step(
                    "docs",
                    "documentation",
                    "Документация",
                    "Подготовить README и документацию по фактическому коду.",
                    after=("pipeline",),
                    writes_code=True,
                    criteria=("Документация соответствует коду",),
                ),
            ),
        ),
        ModePipeline(
            "DEVELOP",
            True,
            "architect",
            (
                Step(
                    "requirements",
                    "requirements",
                    "Критерии приёмки",
                    "Уточнить требования и критерии приёмки изменения.",
                    signal="criteria_missing",
                    discovery=True,
                    criteria=("Критерии приёмки проверяемы",),
                ),
                Step(
                    "database",
                    "database",
                    "Изменение схемы данных",
                    "Подготовить миграции с планом отката.",
                    after=("requirements",),
                    signal="needs_database",
                    writes_code=True,
                    criteria=_C_CHANGE,
                ),
                Step(
                    "main",
                    "developer",
                    "Реализация",
                    "Реализовать изменение с тестами.",
                    after=("requirements", "database"),
                    writes_code=True,
                    criteria=_C_CHANGE,
                ),
                # AGENT_ROLES §4 ставит qa безусловно; ROUTING_POLICY §8 делает qa обязательной с L4, на L2/L3
                # тесты пишет исполнитель и проверяют гейты профиля. Принято правило §8 (см. отчёт P4-09).
                Step(
                    "tests",
                    "qa",
                    "Тесты и тест-план",
                    "Проверить покрытие критериев и добавить недостающие тесты.",
                    after=("main",),
                    min_level=4,
                    writes_code=True,
                    criteria=("Каждый критерий покрыт тестом",),
                ),
                _review(("main", "tests")),
                Step(
                    "security",
                    "security",
                    "Security review",
                    "Проверить изменение на уязвимости.",
                    after=("main",),
                    min_risk="high",
                    criteria=_C_REVIEW,
                ),
                Step(
                    "docs",
                    "documentation",
                    "Документация публичного API",
                    "Обновить документацию изменённого публичного интерфейса.",
                    after=("review",),
                    signal="public_api_change",
                    writes_code=True,
                    criteria=("Документация соответствует коду",),
                ),
            ),
        ),
        ModePipeline(
            "DEBUG",
            True,
            "maintenance",
            (
                Step(
                    "reproduce",
                    "maintenance",
                    "Воспроизведение и диагностика",
                    "Воспроизвести ошибку, добавить падающий тест, провести impact analysis.",
                    criteria=("Есть воспроизводящий тест", "Причина ошибки описана"),
                ),
                Step(
                    "main",
                    "developer",
                    "Исправление",
                    "Исправить ошибку так, чтобы воспроизводящий тест прошёл.",
                    after=("reproduce",),
                    writes_code=True,
                    criteria=_C_CHANGE,
                ),
                Step(
                    "regression",
                    "qa",
                    "Регрессионные тесты",
                    "Проверить регрессию и покрытие исправления.",
                    after=("main",),
                    writes_code=True,
                    criteria=("Регрессионный тест добавлен",),
                ),
                _review(("main", "regression")),
            ),
        ),
        ModePipeline(
            "AUDIT",
            False,
            "architect",
            (
                Step(
                    "architecture",
                    "architect",
                    "Архитектурный анализ",
                    "Проанализировать архитектуру и соответствие требованиям.",
                    criteria=_C_REPORT,
                ),
                Step(
                    "security",
                    "security",
                    "Анализ безопасности",
                    "Проанализировать безопасность и отчёты сканеров.",
                    criteria=_C_REPORT,
                ),
                Step("tests", "qa", "Анализ тестирования", "Проанализировать тесты и покрытие.", criteria=_C_REPORT),
                Step(
                    "performance",
                    "performance",
                    "Анализ производительности",
                    "Оценить производительность воспроизводимыми измерениями.",
                    criteria=_C_REPORT,
                ),
                Step(
                    "review",
                    "reviewer",
                    "Сведение результатов аудита",
                    "Свести и независимо проверить выводы анализов.",
                    after=("architecture", "security", "tests", "performance"),
                    criteria=_C_REVIEW,
                ),
                Step(
                    "report",
                    "documentation",
                    "Отчёт аудита",
                    "Подготовить итоговый отчёт аудита.",
                    after=("review",),
                    criteria=_C_REPORT,
                ),
            ),
        ),
        ModePipeline(
            "MODERNIZE",
            True,
            "architect",
            (
                Step(
                    "requirements",
                    "requirements",
                    "Цели модернизации",
                    "Сформулировать требования и критерии модернизации.",
                    discovery=True,
                    criteria=("Требования проверяемы",),
                ),
                Step(
                    "architecture",
                    "architect",
                    "Целевая архитектура",
                    "Предложить целевую архитектуру, ADR и constraints.",
                    after=("requirements",),
                    discovery=True,
                    criteria=("Черновики ADR и constraints подготовлены",),
                ),
                Step(
                    "impact",
                    "maintenance",
                    "Impact analysis",
                    "Оценить влияние изменений на модули и тесты.",
                    after=("architecture",),
                    criteria=("Затронутые модули и риски описаны",),
                ),
                Step(
                    "database",
                    "database",
                    "Миграции данных",
                    "Подготовить миграции с планом отката.",
                    after=("impact",),
                    signal="needs_database",
                    writes_code=True,
                    criteria=_C_CHANGE,
                ),
                Step(
                    "main",
                    "developer",
                    "Реализация модернизации",
                    "Выполнить модернизацию с тестами.",
                    after=("impact", "database"),
                    writes_code=True,
                    criteria=_C_CHANGE,
                ),
                Step(
                    "tests",
                    "qa",
                    "Тесты",
                    "Проверить покрытие и поведение до и после изменения.",
                    after=("main",),
                    writes_code=True,
                    criteria=("Поведение подтверждено тестами",),
                ),
                _review(("main", "tests")),
                Step(
                    "security",
                    "security",
                    "Security review",
                    "Проверить изменение на уязвимости.",
                    after=("main",),
                    criteria=_C_REVIEW,
                ),
                Step(
                    "pipeline",
                    "devops",
                    "CI/CD",
                    "Обновить конвейер сборки под изменения.",
                    after=("review", "security"),
                    writes_code=True,
                    criteria=_C_CHANGE,
                ),
                Step(
                    "docs",
                    "documentation",
                    "Документация",
                    "Обновить документацию по фактическому коду.",
                    after=("pipeline",),
                    writes_code=True,
                    criteria=("Документация соответствует коду",),
                ),
            ),
        ),
        ModePipeline(
            "SECURITY",
            True,
            "security",
            (
                Step(
                    "threat_model",
                    "security",
                    "Threat model и triage сканеров",
                    "Построить threat model и разобрать отчёты сканеров.",
                    criteria=("Угрозы и находки классифицированы",),
                ),
                Step(
                    "main",
                    "developer",
                    "Исправления безопасности",
                    "Исправить подтверждённые уязвимости с регрессионными тестами.",
                    after=("threat_model",),
                    writes_code=True,
                    criteria=_C_CHANGE,
                ),
                Step(
                    "security_recheck",
                    "security",
                    "Повторная проверка безопасности",
                    "Независимо проверить исправления.",
                    after=("main",),
                    criteria=_C_REVIEW,
                ),
                Step(
                    "tests",
                    "qa",
                    "Тесты",
                    "Проверить регрессионные тесты исправлений.",
                    after=("main",),
                    writes_code=True,
                    criteria=("Исправления покрыты тестами",),
                ),
                _review(("security_recheck", "tests"), min_level=1),
            ),
        ),
        ModePipeline(
            "OPTIMIZE",
            True,
            "performance",
            (
                Step(
                    "baseline",
                    "performance",
                    "Baseline производительности",
                    "Зафиксировать воспроизводимый baseline бенчмарком.",
                    criteria=("Baseline воспроизводим",),
                ),
                Step(
                    "main",
                    "developer",
                    "Оптимизация",
                    "Реализовать оптимизацию без изменения поведения.",
                    after=("baseline",),
                    writes_code=True,
                    criteria=_C_CHANGE,
                ),
                Step(
                    "remeasure",
                    "performance",
                    "Повторное измерение",
                    "Повторить бенчмарк и сравнить с baseline.",
                    after=("main",),
                    criteria=("Эффект измерен на том же окружении",),
                ),
                Step(
                    "tests",
                    "qa",
                    "Тесты",
                    "Проверить, что поведение не изменилось.",
                    after=("main",),
                    writes_code=True,
                    criteria=("Поведение подтверждено тестами",),
                ),
                _review(("remeasure", "tests")),
            ),
        ),
        ModePipeline(
            "TEST",
            True,
            "qa",
            (
                Step(
                    "main",
                    "qa",
                    "Тест-план и тесты",
                    "Составить тест-план и написать недостающие тесты.",
                    writes_code=True,
                    criteria=("Каждый критерий покрыт тестом",),
                ),
                Step(
                    "hooks",
                    "developer",
                    "Test hooks",
                    "Добавить в код точки, необходимые тестам.",
                    after=("main",),
                    signal="needs_test_hooks",
                    writes_code=True,
                    criteria=_C_CHANGE,
                ),
                _review(("main", "hooks")),
            ),
        ),
        ModePipeline(
            "MAINTAIN",
            True,
            "maintenance",
            (
                Step(
                    "impact",
                    "maintenance",
                    "Impact analysis и обновления",
                    "Оценить влияние и обновить зависимости.",
                    writes_code=True,
                    criteria=("Затронутые модули и риски описаны",),
                ),
                Step(
                    "main",
                    "developer",
                    "Адаптация кода",
                    "Адаптировать код к обновлениям с тестами.",
                    after=("impact",),
                    writes_code=True,
                    criteria=_C_CHANGE,
                ),
                Step(
                    "tests",
                    "qa",
                    "Тесты",
                    "Проверить поведение после обновлений.",
                    after=("main",),
                    writes_code=True,
                    criteria=("Поведение подтверждено тестами",),
                ),
                Step(
                    "sca_triage",
                    "security",
                    "Triage SCA",
                    "Разобрать отчёт SCA по обновлённым зависимостям.",
                    after=("main",),
                    criteria=_C_REVIEW,
                ),
                _review(("tests", "sca_triage")),
                Step(
                    "pipeline",
                    "devops",
                    "CI/CD",
                    "Обновить конвейер под изменения.",
                    after=("review",),
                    signal="ci_change",
                    writes_code=True,
                    criteria=_C_CHANGE,
                ),
            ),
        ),
        ModePipeline(
            "DOCUMENT",
            True,
            "architect",
            (
                Step(
                    "main",
                    "documentation",
                    "Документация",
                    "Обновить документацию по фактическому коду.",
                    writes_code=True,
                    criteria=("Документация соответствует коду",),
                ),
                Step(
                    "adr",
                    "architect",
                    "ADR",
                    "Оформить архитектурное решение как ADR.",
                    signal="adr_needed",
                    criteria=("Черновик ADR подготовлен",),
                ),
                _review(("main", "adr")),
            ),
        ),
        ModePipeline(
            "REVIEW",
            False,
            "reviewer",
            (
                Step(
                    "main",
                    "reviewer",
                    "Code review",
                    "Выполнить независимое ревью указанных изменений.",
                    criteria=_C_REVIEW,
                ),
                Step(
                    "security", "security", "Security review", "Проверить изменения на уязвимости.", criteria=_C_REVIEW
                ),
                Step(
                    "tests",
                    "qa",
                    "Анализ тестов",
                    "Проанализировать тесты и покрытие изменений.",
                    signal="tests_review",
                    criteria=_C_REPORT,
                ),
                Step(
                    "report",
                    "documentation",
                    "Сводный отчёт",
                    "Подготовить сводный отчёт ревью.",
                    after=("main", "security", "tests"),
                    criteria=_C_REPORT,
                ),
            ),
        ),
    )
}


class PipelineError(ValueError):
    pass


def effective_level(mode: str, complexity: int) -> int:
    if mode not in PIPELINES:
        raise PipelineError(f"неизвестный режим {mode}")
    if not 1 <= complexity <= 5:
        raise PipelineError("сложность вне 1..5")
    return max(complexity, MODE_MINIMUM[mode])


def _risk_at_least(risk: str, minimum: str) -> bool:
    return RISK_ORDER.index(risk) >= RISK_ORDER.index(minimum)


def _close_deps(steps: list[Step], removed: Mapping[str, tuple[str, ...]]) -> list[Step]:
    """Зависимость от исключённого шага заменяется его (транзитивными) зависимостями."""

    def resolve(key: str, seen: frozenset[str] = frozenset()) -> tuple[str, ...]:
        if key not in removed:
            return (key,)
        if key in seen:  # pragma: no cover — шаблоны ацикличны
            return ()
        out: list[str] = []
        for d in removed[key]:
            out.extend(resolve(d, seen | {key}))
        return tuple(out)

    result = []
    for s in steps:
        deps: list[str] = []
        for d in s.after:
            for r in resolve(d):
                if r not in deps:
                    deps.append(r)
        result.append(replace(s, after=tuple(deps)))
    return result


def _select(pipe: ModePipeline, level: int, risk: str, signals: frozenset[str]) -> tuple[list[Step], list[str]]:
    kept: list[Step] = []
    removed: dict[str, tuple[str, ...]] = {}
    skipped: list[str] = []
    for s in pipe.steps:
        # L4+: задачи security и qa обязательны (ROUTING_POLICY §8) — условия шага на них не действуют.
        forced = level >= 4 and s.role in ("security", "qa")
        reason = None
        if s.signal is not None and s.signal not in signals and not forced:
            reason = f"signal:{s.signal}"
        elif s.min_level > level and not forced:
            reason = f"level<L{s.min_level}"
        elif s.min_risk is not None and not _risk_at_least(risk, s.min_risk) and not forced:
            reason = f"risk<{s.min_risk}"
        if reason:
            removed[s.key] = s.after
            skipped.append(f"{s.key}:{reason}")
        else:
            kept.append(s)
    return _close_deps(kept, removed), skipped


def _entries(steps: list[Step]) -> list[Step]:
    """Первые шаги работы: не discovery и не зависят от других шагов работы."""
    work = {s.key for s in steps if not s.discovery}
    return [s for s in steps if not s.discovery and not any(d in work for d in s.after)]


def _ensure_verifiers(pipe: ModePipeline, steps: list[Step]) -> list[Step]:
    """L4+: в режимах с изменением кода задачи security и qa после основной задачи обязательны."""
    if not pipe.modifies_code:
        return steps
    keys = {s.key for s in steps}
    main = "main" if "main" in keys else steps[-1].key
    roles_after_main = {s.role for s in steps if s.key != main and _depends_on(steps, s.key, main)}
    extra: list[Step] = []
    if "qa" not in roles_after_main and "qa" not in {s.role for s in steps if s.key == main}:
        extra.append(
            Step(
                "tests",
                "qa",
                "Тесты (обязательны с L4)",
                "Проверить покрытие критериев тестами.",
                after=(main,),
                writes_code=True,
                criteria=("Каждый критерий покрыт тестом",),
            )
        )
    if "security" not in roles_after_main:
        extra.append(
            Step(
                "security",
                "security",
                "Security review (обязателен с L4)",
                "Проверить изменение на уязвимости.",
                after=(main,),
                criteria=_C_REVIEW,
            )
        )
    if not extra:
        return steps
    extra = [replace(e, key=_free_key(e.key, keys)) for e in extra]
    new_keys = tuple(e.key for e in extra)
    out = []
    for s in steps:
        if s.role == "reviewer" and s.key != main and main in _ancestors(steps, s.key):
            s = replace(s, after=(*s.after, *new_keys))
        out.append(s)
    pos = next(i for i, s in enumerate(out) if s.key == main) + 1
    return out[:pos] + extra + out[pos:]


def _ensure_final_review(steps: list[Step]) -> list[Step]:
    """L3+: изменения, после которых в конвейере нет проверки (devops/documentation в конце AGENT_ROLES §4),
    закрываются итоговым независимым ревью (QG-REVIEW для этих ролей на L3+, AGENT_ROLES §2.10/§2.12)."""
    unverified = [
        s.key
        for s in steps
        if s.writes_code and not any(o.role in _VERIFIER_ROLES and s.key in _ancestors(steps, o.key) for o in steps)
    ]
    if not unverified:
        return steps
    sinks = tuple(k for k in unverified if not any(k in _ancestors(steps, u) for u in unverified))
    key = _free_key("final_review", {s.key for s in steps})
    final = replace(
        _review(sinks, min_level=1),
        key=key,
        title="Итоговое независимое ревью",
        instruction="Независимо проверить изменения задач, выполненных после основного ревью (CI, документация).",
    )
    return [*steps, final]


def _free_key(key: str, used: set[str]) -> str:
    candidate, n = key, 2
    while candidate in used:
        candidate, n = f"{key}_{n}", n + 1
    used.add(candidate)
    return candidate


def _ancestors(steps: list[Step], key: str) -> set[str]:
    by_key = {s.key: s for s in steps}
    out: set[str] = set()
    stack = list(by_key[key].after)
    while stack:
        k = stack.pop()
        if k not in out and k in by_key:
            out.add(k)
            stack.extend(by_key[k].after)
    return out


def _depends_on(steps: list[Step], key: str, target: str) -> bool:
    return target in _ancestors(steps, key)


def _insert_before_work(steps: list[Step], prelude: list[Step]) -> list[Step]:
    """Вставка цепочки анализа между discovery-шагами и первыми шагами работы."""
    discovery = tuple(s.key for s in steps if s.discovery)
    last = prelude[-1].key
    entries = {s.key for s in _entries(steps)}
    head = [p if p.after else replace(p, after=discovery) for p in prelude]
    out: list[Step] = []
    for s in steps:
        if s.key in entries:
            s = replace(s, after=(*s.after, last))
        out.append(s)
    first_work = next((i for i, s in enumerate(out) if not s.discovery), len(out))
    return out[:first_work] + head + out[first_work:]


def _l4_prelude(pipe: ModePipeline, used: set[str]) -> list[Step]:
    a, b, c = (_free_key(k, used) for k in ("analysis_a", "analysis_b", "analysis_merge"))
    role = pipe.analysis_role
    return [
        Step(
            a,
            role,
            "Независимый анализ задачи (провайдер A)",
            "Проанализировать задачу и предложить подход до реализации.",
            criteria=("Анализ сохранён как артефакт",),
        ),
        Step(
            b,
            role,
            "Независимый анализ задачи (провайдер B)",
            "Проанализировать задачу независимо от другого анализа.",
            criteria=("Анализ сохранён как артефакт",),
        ),
        Step(
            c,
            "planner",
            "Сведение двойного анализа",
            "Свести два независимых анализа, отметить расхождения и уточнить план.",
            after=(a, b),
            criteria=("Расхождения анализов разрешены или вынесены на решение",),
        ),
    ]


def _l5_prelude(pipe: ModePipeline, used: set[str]) -> list[Step]:
    a, b, c = (_free_key(k, used) for k in ("arch_analysis_a", "arch_analysis_b", "arch_merge"))
    steps = [
        Step(
            a,
            "architect",
            "Архитектурный анализ (провайдер A)",
            "Проанализировать архитектурные варианты решения.",
            criteria=("Анализ сохранён как артефакт",),
        ),
        Step(
            b,
            "architect",
            "Архитектурный анализ (провайдер B)",
            "Независимо проанализировать архитектурные варианты.",
            criteria=("Анализ сохранён как артефакт",),
        ),
        Step(
            c,
            "planner",
            "Сведение архитектурного анализа",
            "Свести анализы и выбрать вариант для ADR.",
            after=(a, b),
            criteria=("Выбран вариант с обоснованием",),
        ),
    ]
    if not pipe.modifies_code:
        return steps
    adr, proto, proto_review = (_free_key(k, used) for k in ("adr", "prototype", "prototype_review"))
    return [
        *steps,
        Step(
            adr,
            "architect",
            "ADR решения",
            "Оформить ADR выбранного решения (DECISION_PROPOSAL).",
            after=(c,),
            criteria=("Черновик ADR подготовлен",),
        ),
        Step(
            proto,
            "architect",
            "Прототип",
            "Подготовить прототип в разрешённых путях прототипа.",
            after=(adr,),
            writes_code=True,
            criteria=("Прототип подтверждает решение",),
        ),
        Step(
            proto_review,
            "reviewer",
            "Архитектурное ревью прототипа",
            "Независимо проверить прототип и ADR.",
            after=(proto,),
            criteria=_C_REVIEW,
        ),
    ]


def _instructions(step: Step, objective: str) -> str:
    return f"{step.instruction}\n\nЦель run (данные пользователя):\n{objective}"


def build_pipeline(
    mode: str,
    objective: str,
    *,
    complexity: int = 3,
    risk_level: str = "medium",
    signals: Iterable[str] = (),
) -> PipelinePlan:
    """План по шаблону режима и уровня; результат проходит `dag.validate_plan`."""
    level = effective_level(mode, complexity)
    if risk_level not in RISK_ORDER:
        raise PipelineError(f"неизвестный риск {risk_level}")
    sig = frozenset(signals)
    if sig - SIGNALS:
        raise PipelineError(f"неизвестные сигналы {sorted(sig - SIGNALS)}")
    pipe = PIPELINES[mode]
    steps, skipped = _select(pipe, level, risk_level, sig)
    if level >= 4:
        steps = _ensure_verifiers(pipe, steps)
    if level >= 3 and pipe.modifies_code:
        steps = _ensure_final_review(steps)
    used = {s.key for s in steps}
    approvals: list[ApprovalPoint] = []
    entry_keys = tuple(s.key for s in _entries(steps))
    if level == 4 and pipe.modifies_code:
        prelude = _l4_prelude(pipe, used)
        steps = _insert_before_work(steps, prelude)
        approvals.append(
            ApprovalPoint("PLAN_APPROVAL", entry_keys, prelude[-1].key, True, "L4: план и сведение двойного анализа")
        )
    elif level == 5:
        prelude = _l5_prelude(pipe, used)
        steps = _insert_before_work(steps, prelude)
        if pipe.modifies_code:
            adr, proto, proto_review = (p.key for p in prelude[3:6])
            approvals.append(
                ApprovalPoint("PLAN_APPROVAL", (proto,), adr, True, "L5: план и утверждение ADR до реализации")
            )
            approvals.append(
                ApprovalPoint("MANUAL_REVIEW", entry_keys, proto_review, True, "L5: решение по результатам прототипа")
            )
        else:
            approvals.append(
                ApprovalPoint("PLAN_APPROVAL", entry_keys, prelude[-1].key, True, "L5: план и сведение анализа")
            )
    elif level == 4:
        approvals.append(ApprovalPoint("PLAN_APPROVAL", (), None, True, "L4: утверждение плана"))
    if level >= 4 and pipe.modifies_code:
        approvals.append(
            ApprovalPoint("MERGE", (), None, _risk_at_least(risk_level, "high") or level == 5, "результат перед MERGE")
        )
    tasks: list[PipelineTask] = []
    for s in steps:
        title = f"{mode}: {objective[:200]}" if s.key == "main" else s.title
        tasks.append(
            PipelineTask(
                key=s.key,
                role=s.role,
                title=title[:500] or s.title,
                instructions=_instructions(s, objective),
                depends_on=s.after,
                acceptance_criteria=s.criteria or _C_CHANGE,
                writes_code=s.writes_code,
            )
        )
    plan = PipelinePlan(mode, level, risk_level, pipe.modifies_code, tuple(tasks), tuple(approvals), tuple(skipped))
    validate_plan([PlannedTask(t.key, t.role, level, t.depends_on) for t in plan.tasks])
    return plan
