"""P6-02: матрица профилей basic/strict/critical, применимость по стеку (repo facts + project_technologies),
план gates задачи (required vs advisory) и реестр провайдеров."""

from __future__ import annotations

import json

import pytest

from aicd_verification import registry
from aicd_verification.gates import (
    Applicability,
    DiffEntry,
    GateProvider,
    GateSpec,
    RepoFacts,
    StackInfo,
)
from aicd_verification.profiles import (
    GATE_TYPES,
    PROFILE_MATRIX,
    GateRow,
    level,
    plan_gates,
    thresholds,
    tool_missing_effect,
)


def facts(files: set[str], texts: dict[str, str] | None = None, changed: dict[str, str] | None = None) -> RepoFacts:
    changes = tuple(DiffEntry(p, "A", "000000", "100644", "0" * 40) for p in (changed or {}))
    return RepoFacts(frozenset(files), texts or {}, changes, changed or {})


def stack(*pairs: tuple[str, str]) -> StackInfo:
    return StackInfo(frozenset(pairs))


# --- Матрица (QUALITY_GATES §2.2) -------------------------------------------------------------------

EXPECTED = {
    "FORMAT": ("R", "R", "R"),
    "LINT": ("R", "R", "R"),
    "TYPECHECK": ("R*", "R", "R"),
    "BUILD": ("R", "R", "R"),
    "UNIT": ("R", "R", "R"),
    "INTEGRATION": ("R*", "R", "R"),
    "E2E": ("R*", "R", "R"),
    "SAST": ("R", "R", "R"),
    "SCA": ("R", "R", "R"),
    "SECRETS": ("R", "R", "R"),
    "IAC": ("O", "R*", "R"),
    "POLICY": ("-", "O", "R*"),
    "MIGRATION": ("R*", "R*", "R*"),
    "PERF": ("R*", "R*", "R*"),
    "REVIEW": ("R", "R", "R"),
    "COVERAGE": ("O", "R", "R"),
    "SBOM": ("-", "O", "R"),
    "SIGN": ("-", "-", "R*"),
    "SCOPE": ("R", "R", "R"),
}


@pytest.mark.parametrize("gate_type", GATE_TYPES)
def test_profile_matrix_matches_quality_gates_doc(gate_type: str) -> None:
    assert tuple(PROFILE_MATRIX[p][gate_type] for p in ("basic", "strict", "critical")) == EXPECTED[gate_type]


def test_unknown_profile_is_treated_as_critical() -> None:
    assert level("nonexistent", "SBOM") == "R"
    assert thresholds("nonexistent").finding_block_severity == "medium"


def test_thresholds_per_profile_and_definition_only_tightens() -> None:
    assert thresholds("basic").finding_block_severity == "critical" and thresholds("basic").coverage_min_percent is None
    assert thresholds("strict").finding_block_severity == "high" and thresholds("strict").coverage_min_percent == 85
    assert thresholds("critical").finding_block_severity == "medium"
    tightened = thresholds("strict", {"finding_block_severity": "medium", "coverage_min_percent": 90})
    assert tightened.finding_block_severity == "medium" and tightened.coverage_min_percent == 90
    loosened = thresholds("strict", {"finding_block_severity": "critical", "coverage_min_percent": 10})
    assert loosened.finding_block_severity == "high" and loosened.coverage_min_percent == 85


@pytest.mark.parametrize(
    ("profile", "gate_type", "effect"),
    [
        ("basic", "UNIT", "MANUAL_REVIEW"),
        ("strict", "LINT", "MANUAL_REVIEW"),
        ("strict", "UNIT", "FAIL"),
        ("strict", "MIGRATION", "FAIL"),
        ("critical", "FORMAT", "FAIL"),
    ],
)
def test_tool_missing_effect(profile: str, gate_type: str, effect: str) -> None:
    assert tool_missing_effect(profile, gate_type) == effect


# --- Применимость по стеку -------------------------------------------------------------------------


def test_js_stack_applicability_from_markers() -> None:
    pkg = {"devDependencies": {"vitest": "3", "typescript": "5"}, "scripts": {"build": "tsc -b"}}
    f = facts(
        {"package.json", "tsconfig.json", "src/a.ts", "eslint.config.mjs", "playwright.config.ts"},
        {"package.json": json.dumps(pkg)},
    )
    app = registry.detect_all(f)
    for name in ("eslint", "tsc", "vitest", "playwright", "npm-build"):
        assert app[name].applicable, (name, app[name])
    for name in ("mypy", "pyright", "phpstan", "python-build", "pytest-coverage", "migrations", "ruff-lint"):
        assert not app[name].applicable and app[name].reason and app[name].markers


def test_stack_from_project_technologies_extends_markers() -> None:
    f = facts({"package.json"}, {"package.json": "{}"})
    assert not registry.detect_all(f)["vitest"].applicable
    # vitest известен только из project_technologies (индексатор Phase 5)
    assert registry.detect_all(f, stack(("npm", "vitest")))["vitest"].applicable
    # TypeScript в стеке без tsconfig.json → неприменим, причина называет проблему
    tsc = registry.detect_all(facts(set()), stack(("lang", "typescript")))["tsc"]
    assert not tsc.applicable and "without tsconfig" in tsc.reason
    # eslint зависимость без конфигурации — причина явно указывает на отсутствие конфигурации
    es = registry.detect_all(facts({"a.js"}), stack(("npm", "eslint")))["eslint"]
    assert not es.applicable and "without an ESLint configuration" in es.reason


def test_python_stack_applicability() -> None:
    texts = {"pyproject.toml": "[build-system]\nrequires=['hatchling']\n\n[tool.mypy]\nstrict = true\n"}
    f = facts({"pyproject.toml", "pkg/a.py", "tests/test_a.py", "pyrightconfig.json"}, texts)
    app = registry.detect_all(f)
    assert app["mypy"].applicable and "pyproject.toml [tool.mypy]" in app["mypy"].markers
    assert app["pyright"].applicable and app["python-build"].applicable and app["pytest-coverage"].applicable
    assert registry.detect_all(facts({"a.py", "setup.cfg"}, {"setup.cfg": "[mypy]\n"}))["mypy"].applicable


def test_php_stack_requires_phpstan_config() -> None:
    assert not registry.detect_all(facts({"composer.json", "src/a.php"}))["phpstan"].applicable
    assert registry.detect_all(facts({"composer.json", "src/a.php", "phpstan.neon.dist"}))["phpstan"].applicable
    assert registry.detect_all(facts({"phpstan.neon"}), stack(("lang", "php")))["phpstan"].applicable


def test_migrations_applicability() -> None:
    sql = facts({"db/migrations/0001_a.up.sql", "db/migrations/0001_a.down.sql"})
    assert registry.detect_all(sql)["migrations"].applicable
    alembic = facts({"alembic.ini", "alembic/env.py"}, {"alembic.ini": "[alembic]\nscript_location = alembic\n"})
    assert "alembic" in registry.detect_all(alembic)["migrations"].reason


def test_failing_detector_keeps_gates_in_plan() -> None:
    def boom(_f: RepoFacts, _s: StackInfo) -> dict[str, Applicability]:
        raise RuntimeError("detector bug")

    spec = registry.GATE_SPECS["eslint"]
    provider = GateProvider("broken", (spec,), boom)
    assert registry.detect_all(facts(set()), providers=(provider,)) == {}
    plan = plan_gates("strict", [GateRow(1, "eslint", "LINT", True)], {})
    assert plan.gates[0].disposition == "run" and plan.gates[0].reason == "applicability unknown"  # fail-closed


# --- План gates ------------------------------------------------------------------------------------

ROWS = [
    GateRow(1, "scope", "SCOPE", False),
    GateRow(2, "eslint", "LINT", True),
    GateRow(3, "tsc", "TYPECHECK", False),
    GateRow(4, "pytest-coverage", "COVERAGE", False),
    GateRow(5, "conftest-policy", "POLICY", False),
    GateRow(6, "syft-sbom", "SBOM", False),
    GateRow(7, "migrations", "MIGRATION", False),
]
APP = {
    "scope": Applicability(True, "always"),
    "eslint": Applicability(True, "config"),
    "tsc": Applicability(True, "tsconfig"),
    "pytest-coverage": Applicability(True, "tests"),
    "conftest-policy": Applicability(True, "rego"),
    "syft-sbom": Applicability(True, "release"),
    "migrations": Applicability(False, "no migrations", ("**/migrations/**",)),
}


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (
            "basic",
            {
                "scope": ("run", True),
                "eslint": ("run", True),
                "tsc": ("run", True),
                "pytest-coverage": ("run", False),  # O — advisory
                "conftest-policy": ("not_applicable", False),  # «-» в basic
                "syft-sbom": ("not_applicable", False),
                "migrations": ("not_applicable", True),
            },
        ),
        (
            "strict",
            {
                "scope": ("run", True),
                "eslint": ("run", True),
                "tsc": ("run", True),
                "pytest-coverage": ("run", True),
                "conftest-policy": ("run", False),
                "syft-sbom": ("run", False),
                "migrations": ("not_applicable", True),
            },
        ),
        (
            "critical",
            {
                "scope": ("run", True),
                "eslint": ("run", True),
                "tsc": ("run", True),
                "pytest-coverage": ("run", True),
                "conftest-policy": ("run", True),
                "syft-sbom": ("run", True),
                "migrations": ("not_applicable", True),
            },
        ),
    ],
)
def test_plan_matrix(profile: str, expected: dict[str, tuple[str, bool]]) -> None:
    plan = plan_gates(profile, ROWS, APP, known_gates=[r.name for r in ROWS])
    got = {g.row.name: (g.disposition, g.required) for g in plan.gates}
    assert got == expected
    for g in plan.gates:
        assert g.reason  # NOT_APPLICABLE и run всегда с причиной
    report = plan.as_report()
    assert report["profile"] == profile and len(report["gates"]) == len(ROWS)
    assert set(plan.required_names) | set(plan.advisory_names) == {n for n, (d, _r) in got.items() if d == "run"}


def test_plan_row_required_tightens_advisory_and_excluded_gates() -> None:
    rows = [GateRow(1, "pytest-coverage", "COVERAGE", True), GateRow(2, "syft-sbom", "SBOM", True)]
    plan = plan_gates("basic", rows, APP)
    assert all(g.required and g.disposition == "run" for g in plan.gates)


def test_unknown_gate_stays_in_plan() -> None:
    plan = plan_gates("strict", [GateRow(1, "mystery", "LINT", True)], {}, known_gates=["eslint"])
    assert plan.gates[0].disposition == "run" and "no runner" in plan.gates[0].reason


# --- Реестр ----------------------------------------------------------------------------------------


def test_registry_has_unique_names_and_definitions_for_all_specs() -> None:
    names = [d[0] for d in registry.definitions()]
    assert len(names) == len(set(names))
    core_and_quality = {"scope", "ruff-lint", "eslint", "phpstan", "mypy", "tsc", "npm-build", "vitest", "playwright"}
    assert core_and_quality | {"pytest-coverage", "migrations"} <= set(registry.GATE_SPECS)
    assert set(names) <= set(registry.GATE_SPECS)
    with pytest.raises(ValueError, match="more than one provider"):
        registry.build_registry([registry.PROVIDERS[0], registry.PROVIDERS[0]])


def test_seed_rows_follow_profile_matrix() -> None:
    rows = registry.seed_rows()
    by = {(r["profile"], r["name"]): r for r in rows}
    assert by[("basic", "pytest-coverage")]["required"] is False
    assert by[("strict", "pytest-coverage")]["required"] is True
    assert by[("basic", "tsc")]["required"] is True and by[("basic", "tsc")]["level"] == "R*"
    assert all(r["required"] for r in rows if r["gate_type"] == "SCOPE")


def test_provider_from_adapts_single_argument_detector() -> None:
    spec: GateSpec = registry.GATE_SPECS["eslint"]
    provider = registry.provider_from("x", [spec], lambda f: {"eslint": Applicability(bool(f.files), "files")})
    assert provider.detect(facts({"a"}), StackInfo())["eslint"].applicable


def test_stack_info_from_rows() -> None:
    info = StackInfo.from_rows([{"ecosystem": "NPM", "name": "Vitest"}, {"ecosystem": None, "name": "x"}])
    assert info.has("npm", "vitest") and len(info.technologies) == 1
