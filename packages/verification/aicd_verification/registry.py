"""Реестр gates Verification Engine (P6-02/P6-03): провайдеры → `GATE_SPECS` + детектор применимости.

Точка расширения (hook) для других модулей gates — кортеж `PROVIDERS`. Провайдер — это
`aicd_verification.gates.GateProvider(name, specs, detect, definitions)`:

* `specs` — `GateSpec` (имя = `quality_gates.name`; позиционная форма v0 + необязательные поля
  Phase 6: `evaluate(code, stdout, GateContext) -> Verdict` с нормализованными `findings`,
  `image_key`, `probe_for`, `container_user`, `needs_input_dir`);
* `detect(facts: RepoFacts, stack: StackInfo) -> {name: Applicability}` — применимость своих gates
  (NOT_APPLICABLE только с причиной и маркерами);
* `definitions` — (name, gate_type, timeout_seconds, description) для seed `quality_gates`.

Подключение security/supply-chain gates (P6-04/P6-07, модуль `security_gates.py`):

    from aicd_verification import security_gates
    PROVIDERS = (CORE_PROVIDER, runners.PROVIDER, migrations.PROVIDER, security_gates.SECURITY_PROVIDER)

Если модуль экспортирует только `SPECS` и функцию детектора, провайдер собирается адаптером
`provider_from(name, specs, detect)` (детектор с сигнатурой `(facts)` или `(facts, stack)`).
Имена gates между провайдерами уникальны (`build_registry` проверяет это при импорте).

`required` по профилю для seed: `registry.seed_rows()` — матрица QUALITY_GATES §2.2 × определения.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from aicd_verification import migrations, runners
from aicd_verification.gates import (
    CORE_PROVIDER,
    EMPTY_STACK,
    Applicability,
    GateProvider,
    GateSpec,
    RepoFacts,
    StackInfo,
)
from aicd_verification.security_gates import SECURITY_PROVIDER

log = logging.getLogger("aicd.verification")


def provider_from(
    name: str,
    specs: Iterable[GateSpec],
    detect: Callable[..., Mapping[str, Applicability]],
    definitions: Iterable[tuple[str, str, int, str]] = (),
) -> GateProvider:
    """Адаптер для модулей, экспортирующих SPECS + детектор `(facts)` или `(facts, stack)`."""
    arity = len(inspect.signature(detect).parameters)

    def _detect(facts: RepoFacts, stack: StackInfo) -> Mapping[str, Applicability]:
        return detect(facts, stack) if arity >= 2 else detect(facts)

    return GateProvider(name, tuple(specs), _detect, tuple(definitions))


PROVIDERS: tuple[GateProvider, ...] = (
    CORE_PROVIDER,
    runners.PROVIDER,
    migrations.PROVIDER,
    SECURITY_PROVIDER,  # SAST/SECRETS/SCA/IAC/POLICY/PERF/SBOM/SIGN (P6-04, P6-06, P6-07)
)


def build_registry(providers: Iterable[GateProvider]) -> dict[str, GateSpec]:
    specs: dict[str, GateSpec] = {}
    for provider in providers:
        for spec in provider.specs:
            if spec.name in specs:
                raise ValueError(f"gate {spec.name!r} is defined by more than one provider ({provider.name})")
            specs[spec.name] = spec
    return specs


GATE_SPECS: dict[str, GateSpec] = build_registry(PROVIDERS)


def detect_all(
    facts: RepoFacts, stack: StackInfo = EMPTY_STACK, providers: Iterable[GateProvider] = PROVIDERS
) -> dict[str, Applicability]:
    """Применимость всех зарегистрированных gates. Ошибка детектора провайдера не скрывает gates:
    они получают «applicability unknown» → план выполняет их (fail-closed)."""
    result: dict[str, Applicability] = {}
    for provider in providers:
        try:
            found = provider.detect(facts, stack)
        except Exception:  # детектор не должен ронять верификацию; gates останутся в плане
            log.exception("gate applicability detector %s failed", provider.name)
            continue
        for name, app in found.items():
            if name in {s.name for s in provider.specs}:
                result[name] = app
    return result


def definitions(providers: Iterable[GateProvider] = PROVIDERS) -> tuple[tuple[str, str, int, str], ...]:
    out: list[tuple[str, str, int, str]] = []
    for provider in providers:
        out.extend(provider.definitions)
    return tuple(out)


def seed_rows(providers: Iterable[GateProvider] = PROVIDERS) -> list[dict[str, Any]]:
    """Строки seed `quality_gates` для глобальных профилей: required = уровень R/R* матрицы профиля."""
    from aicd_verification.profiles import PROFILES, level

    rows: list[dict[str, Any]] = []
    for profile in PROFILES:
        for name, gate_type, timeout, description in definitions(providers):
            lvl = "R" if gate_type == "SCOPE" else level(profile, gate_type)
            if lvl == "-":
                continue  # gate не входит в профиль
            rows.append(
                {
                    "profile": profile,
                    "name": name,
                    "gate_type": gate_type,
                    "required": lvl in ("R", "R*"),
                    "timeout_seconds": timeout,
                    "description": description,
                    "level": lvl,
                }
            )
    return rows
