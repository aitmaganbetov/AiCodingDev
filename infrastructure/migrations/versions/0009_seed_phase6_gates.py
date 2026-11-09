"""Seed: определения quality gates Phase 6 для глобальных профилей basic/strict/critical.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-08

Источник строк — `aicd_verification.registry.seed_rows()` (матрица QUALITY_GATES §2.2 × определения
всех провайдеров gates: качество, миграции, безопасность, производительность, цепочка поставок), снятый
в статический список: миграция не импортирует код приложения. Тест `test_gate_seed_matches_registry`
сверяет seed с реестром, расхождение ломает CI.

Уровень матрицы: R — обязателен; R* — обязателен, если применим (применимость решает движок);
O — рекомендован (required=false, не блокирует). Существующие строки v0 (0004) обновляются по матрице.
Таблицы под FORCE RLS: на время миграции FORCE снимается (как в 0004) и возвращается в той же транзакции.
"""

import json

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

# (profile, name, gate_type, required, timeout_seconds, level, description)
GATES = (
    (
        "basic",
        "scope",
        "SCOPE",
        True,
        600,
        "R",
        "Изменения только внутри worktree, без .git/symlink-побегов и секретов",
    ),
    ("basic", "ruff-lint", "LINT", True, 600, "R", "ruff check (Python), если в репозитории есть конфигурация ruff"),
    ("basic", "ruff-format", "FORMAT", True, 600, "R", "ruff format --check (Python)"),
    ("basic", "pytest-unit", "UNIT", True, 600, "R", "pytest, если есть тесты Python"),
    ("basic", "npm-unit", "UNIT", True, 600, "R", "npm test, если в package.json есть скрипт test"),
    ("basic", "eslint", "LINT", True, 900, "R", "ESLint (JS/TS), если есть конфигурация ESLint"),
    ("basic", "phpstan", "TYPECHECK", True, 900, "R*", "PHPStan, если есть phpstan.neon"),
    ("basic", "mypy", "TYPECHECK", True, 900, "R*", "mypy, если mypy сконфигурирован"),
    ("basic", "pyright", "TYPECHECK", True, 900, "R*", "pyright, если pyright сконфигурирован"),
    ("basic", "tsc", "TYPECHECK", True, 900, "R*", "tsc --noEmit, если есть tsconfig.json"),
    ("basic", "npm-build", "BUILD", True, 1800, "R", "npm run build (в контейнере, копия дерева)"),
    ("basic", "python-build", "BUILD", True, 1800, "R", "python -m build --no-isolation (в контейнере, копия дерева)"),
    ("basic", "vitest", "UNIT", True, 900, "R", "Vitest, если в package.json есть зависимость vitest"),
    ("basic", "playwright", "E2E", True, 1800, "R*", "Playwright, только при playwright.config.*; сеть none"),
    ("basic", "pytest-coverage", "COVERAGE", False, 1800, "O", "pytest-cov: line coverage core ≥ порога профиля"),
    (
        "basic",
        "migrations",
        "MIGRATION",
        True,
        1200,
        "R*",
        "Миграции: upgrade→downgrade→upgrade на эфемерной PostgreSQL; деструктивные — approval",
    ),
    ("basic", "semgrep-sast", "SAST", True, 900, "R", "SAST: Semgrep, ruleset платформы"),
    ("basic", "gitleaks-secrets", "SECRETS", True, 600, "R", "SECRETS: Gitleaks по файлам commit"),
    ("basic", "trivy-vuln", "SCA", True, 600, "R", "SCA: Trivy fs (офлайн-БД уязвимостей)"),
    ("basic", "osv-scanner-sca", "SCA", True, 600, "R", "SCA: OSV-Scanner по lock-файлам (офлайн-БД)"),
    ("basic", "trivy-config", "IAC", False, 600, "O", "IAC: Trivy misconfig + secret"),
    ("basic", "checkov-iac", "IAC", False, 900, "O", "IAC: Checkov"),
    (
        "basic",
        "bench-perf",
        "PERF",
        True,
        1800,
        "R*",
        "PERF: воспроизводимый бенчмарк baseline vs candidate при заявлении об ускорении",
    ),
    (
        "strict",
        "scope",
        "SCOPE",
        True,
        600,
        "R",
        "Изменения только внутри worktree, без .git/symlink-побегов и секретов",
    ),
    ("strict", "ruff-lint", "LINT", True, 600, "R", "ruff check (Python), если в репозитории есть конфигурация ruff"),
    ("strict", "ruff-format", "FORMAT", True, 600, "R", "ruff format --check (Python)"),
    ("strict", "pytest-unit", "UNIT", True, 600, "R", "pytest, если есть тесты Python"),
    ("strict", "npm-unit", "UNIT", True, 600, "R", "npm test, если в package.json есть скрипт test"),
    ("strict", "eslint", "LINT", True, 900, "R", "ESLint (JS/TS), если есть конфигурация ESLint"),
    ("strict", "phpstan", "TYPECHECK", True, 900, "R", "PHPStan, если есть phpstan.neon"),
    ("strict", "mypy", "TYPECHECK", True, 900, "R", "mypy, если mypy сконфигурирован"),
    ("strict", "pyright", "TYPECHECK", True, 900, "R", "pyright, если pyright сконфигурирован"),
    ("strict", "tsc", "TYPECHECK", True, 900, "R", "tsc --noEmit, если есть tsconfig.json"),
    ("strict", "npm-build", "BUILD", True, 1800, "R", "npm run build (в контейнере, копия дерева)"),
    ("strict", "python-build", "BUILD", True, 1800, "R", "python -m build --no-isolation (в контейнере, копия дерева)"),
    ("strict", "vitest", "UNIT", True, 900, "R", "Vitest, если в package.json есть зависимость vitest"),
    ("strict", "playwright", "E2E", True, 1800, "R", "Playwright, только при playwright.config.*; сеть none"),
    ("strict", "pytest-coverage", "COVERAGE", True, 1800, "R", "pytest-cov: line coverage core ≥ порога профиля"),
    (
        "strict",
        "migrations",
        "MIGRATION",
        True,
        1200,
        "R*",
        "Миграции: upgrade→downgrade→upgrade на эфемерной PostgreSQL; деструктивные — approval",
    ),
    ("strict", "semgrep-sast", "SAST", True, 900, "R", "SAST: Semgrep, ruleset платформы"),
    ("strict", "gitleaks-secrets", "SECRETS", True, 600, "R", "SECRETS: Gitleaks по файлам commit"),
    ("strict", "trivy-vuln", "SCA", True, 600, "R", "SCA: Trivy fs (офлайн-БД уязвимостей)"),
    ("strict", "osv-scanner-sca", "SCA", True, 600, "R", "SCA: OSV-Scanner по lock-файлам (офлайн-БД)"),
    ("strict", "trivy-config", "IAC", True, 600, "R*", "IAC: Trivy misconfig + secret"),
    ("strict", "checkov-iac", "IAC", True, 900, "R*", "IAC: Checkov"),
    ("strict", "conftest-policy", "POLICY", False, 300, "O", "POLICY: conftest/OPA по политикам *.rego"),
    (
        "strict",
        "bench-perf",
        "PERF",
        True,
        1800,
        "R*",
        "PERF: воспроизводимый бенчмарк baseline vs candidate при заявлении об ускорении",
    ),
    ("strict", "syft-sbom", "SBOM", False, 600, "O", "SBOM: Syft → CycloneDX JSON"),
    (
        "critical",
        "scope",
        "SCOPE",
        True,
        600,
        "R",
        "Изменения только внутри worktree, без .git/symlink-побегов и секретов",
    ),
    ("critical", "ruff-lint", "LINT", True, 600, "R", "ruff check (Python), если в репозитории есть конфигурация ruff"),
    ("critical", "ruff-format", "FORMAT", True, 600, "R", "ruff format --check (Python)"),
    ("critical", "pytest-unit", "UNIT", True, 600, "R", "pytest, если есть тесты Python"),
    ("critical", "npm-unit", "UNIT", True, 600, "R", "npm test, если в package.json есть скрипт test"),
    ("critical", "eslint", "LINT", True, 900, "R", "ESLint (JS/TS), если есть конфигурация ESLint"),
    ("critical", "phpstan", "TYPECHECK", True, 900, "R", "PHPStan, если есть phpstan.neon"),
    ("critical", "mypy", "TYPECHECK", True, 900, "R", "mypy, если mypy сконфигурирован"),
    ("critical", "pyright", "TYPECHECK", True, 900, "R", "pyright, если pyright сконфигурирован"),
    ("critical", "tsc", "TYPECHECK", True, 900, "R", "tsc --noEmit, если есть tsconfig.json"),
    ("critical", "npm-build", "BUILD", True, 1800, "R", "npm run build (в контейнере, копия дерева)"),
    (
        "critical",
        "python-build",
        "BUILD",
        True,
        1800,
        "R",
        "python -m build --no-isolation (в контейнере, копия дерева)",
    ),
    ("critical", "vitest", "UNIT", True, 900, "R", "Vitest, если в package.json есть зависимость vitest"),
    ("critical", "playwright", "E2E", True, 1800, "R", "Playwright, только при playwright.config.*; сеть none"),
    ("critical", "pytest-coverage", "COVERAGE", True, 1800, "R", "pytest-cov: line coverage core ≥ порога профиля"),
    (
        "critical",
        "migrations",
        "MIGRATION",
        True,
        1200,
        "R*",
        "Миграции: upgrade→downgrade→upgrade на эфемерной PostgreSQL; деструктивные — approval",
    ),
    ("critical", "semgrep-sast", "SAST", True, 900, "R", "SAST: Semgrep, ruleset платформы"),
    ("critical", "gitleaks-secrets", "SECRETS", True, 600, "R", "SECRETS: Gitleaks по файлам commit"),
    ("critical", "trivy-vuln", "SCA", True, 600, "R", "SCA: Trivy fs (офлайн-БД уязвимостей)"),
    ("critical", "osv-scanner-sca", "SCA", True, 600, "R", "SCA: OSV-Scanner по lock-файлам (офлайн-БД)"),
    ("critical", "trivy-config", "IAC", True, 600, "R", "IAC: Trivy misconfig + secret"),
    ("critical", "checkov-iac", "IAC", True, 900, "R", "IAC: Checkov"),
    ("critical", "conftest-policy", "POLICY", True, 300, "R*", "POLICY: conftest/OPA по политикам *.rego"),
    (
        "critical",
        "bench-perf",
        "PERF",
        True,
        1800,
        "R*",
        "PERF: воспроизводимый бенчмарк baseline vs candidate при заявлении об ускорении",
    ),
    ("critical", "syft-sbom", "SBOM", True, 600, "R", "SBOM: Syft → CycloneDX JSON"),
    ("critical", "cosign-verify", "SIGN", True, 300, "R*", "SIGN: Cosign verify ключом политики (офлайн)"),
)
_RLS_TABLES = ("quality_profiles", "quality_gates")


def _force_rls(enabled: bool) -> None:
    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE {table} {'' if enabled else 'NO '}FORCE ROW LEVEL SECURITY")


def _values() -> str:
    rows = []
    for profile, name, gate_type, required, timeout, level, description in GATES:
        cfg = json.dumps({"description": description, "seed": "p6", "level": level}, ensure_ascii=False)
        rows.append(
            "  ('{p}', '{n}', '{t}', {r}, {to}, '{c}')".format(
                p=profile, n=name, t=gate_type, r="true" if required else "false", to=timeout, c=cfg.replace("'", "''")
            )
        )
    return ",\n".join(rows)


def upgrade() -> None:
    _force_rls(False)
    op.execute(
        "INSERT INTO quality_gates (id, workspace_id, profile_id, name, gate_type, required, config, timeout_seconds)\n"
        "SELECT overlay(overlay(md5(random()::text || p.id::text || g.name) PLACING '7' FROM 13)"
        " PLACING '8' FROM 17)::uuid, NULL, p.id, g.name, g.gate_type, g.required, g.cfg::jsonb, g.timeout\n"
        f"FROM quality_profiles p JOIN (VALUES\n{_values()}\n) AS g(profile, name, gate_type, required, timeout, cfg)"
        " ON g.profile = p.code\n"
        "WHERE p.workspace_id IS NULL AND p.version = 1\n"
        "ON CONFLICT ON CONSTRAINT uq_quality_gates_profile_name DO UPDATE"
        " SET required = EXCLUDED.required, gate_type = EXCLUDED.gate_type, timeout_seconds = EXCLUDED.timeout_seconds"
    )
    op.execute(
        "DO $$ BEGIN IF (SELECT count(*) FROM quality_gates WHERE workspace_id IS NULL AND config->>'seed' IN ('v0','p6'))"  # noqa: S608 — только константы миграции
        " < "
        + str(len({(g[0], g[1]) for g in GATES}))
        + " THEN RAISE EXCEPTION 'seed quality gates p6: строк меньше ожидаемого';"
        " END IF; END $$"
    )
    _force_rls(True)


def downgrade() -> None:
    _force_rls(False)
    names = ",".join(
        "'" + n + "'"
        for n in sorted({g[1] for g in GATES} - {"scope", "ruff-lint", "ruff-format", "pytest-unit", "npm-unit"})
    )
    op.execute(f"DELETE FROM quality_gates WHERE workspace_id IS NULL AND config->>'seed' = 'p6' AND name IN ({names})")  # noqa: S608 — только константы миграции
    _force_rls(True)
