"""Seed: определения quality gates v0 для глобальных профилей basic/strict/critical.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-08

Источник: packages/verification/aicd_verification/gates.py V0_GATE_DEFINITIONS (P3-14).
Применимость gate определяется Verification Engine по содержимому репозитория на коммите;
SCOPE применим всегда. Phase 6 (P6-02) расширит набор гейтов и профили.
Обратимость: downgrade удаляет только эти seed-строки глобальных профилей.

Таблицы под FORCE ROW LEVEL SECURITY, а у aicd_migrator нет политик: без снятия FORCE на время
транзакции миграции INSERT … SELECT молча видит 0 профилей. FORCE возвращается в той же транзакции.
"""

import json

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

GATES = (
    ("scope", "SCOPE", "Изменения только внутри worktree, без .git/symlink-побегов и секретов"),
    ("ruff-lint", "LINT", "ruff check (Python), если в репозитории есть конфигурация ruff"),
    ("ruff-format", "FORMAT", "ruff format --check (Python)"),
    ("pytest-unit", "UNIT", "pytest, если есть тесты Python"),
    ("npm-unit", "UNIT", "npm test, если в package.json есть скрипт test"),
)
TIMEOUT_SECONDS = 600


_RLS_TABLES = ("quality_profiles", "quality_gates")


def _force_rls(enabled: bool) -> None:
    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE {table} {'' if enabled else 'NO '}FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    _force_rls(False)
    # Один SQL-запрос (работает и в offline-режиме `alembic upgrade --sql`). ID — формата UUIDv7
    # (версия 7, вариант RFC 4122); монотонность для справочных seed-строк не требуется.
    values = ",\n".join(
        "  ('{n}', '{t}', '{cfg}')".format(
            n=name, t=gate_type, cfg=json.dumps({"description": d, "seed": "v0"}, ensure_ascii=False).replace("'", "''")
        )
        for name, gate_type, d in GATES
    )
    op.execute(
        "INSERT INTO quality_gates (id, workspace_id, profile_id, name, gate_type, required, config, timeout_seconds)\n"
        "SELECT overlay(overlay(md5(random()::text || p.id::text || g.name) PLACING '7' FROM 13)"
        " PLACING '8' FROM 17)::uuid, NULL, p.id, g.name, g.gate_type, true, g.cfg::jsonb, "
        f"{TIMEOUT_SECONDS}\n"
        f"FROM quality_profiles p CROSS JOIN (VALUES\n{values}\n) AS g(name, gate_type, cfg)\n"
        "WHERE p.workspace_id IS NULL AND p.version = 1\n"
        "ON CONFLICT ON CONSTRAINT uq_quality_gates_profile_name DO NOTHING"
    )
    op.execute(
        "DO $$ BEGIN IF (SELECT count(*) FROM quality_gates WHERE config->>'seed' = 'v0' AND workspace_id IS NULL) = 0"
        " THEN RAISE EXCEPTION 'seed quality gates v0: не вставлено ни одной строки'; END IF; END $$"
    )
    _force_rls(True)


def downgrade() -> None:
    _force_rls(False)
    op.execute(
        "DELETE FROM quality_gates WHERE workspace_id IS NULL AND config->>'seed' = 'v0'"
        " AND name IN ('scope','ruff-lint','ruff-format','pytest-unit','npm-unit')"
    )
    _force_rls(True)
