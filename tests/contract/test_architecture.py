"""P1-05: каждая таблица принадлежит ровно одному модулю; границы импортов проверяет import-linter."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

import aicd_cli_adapters
import aicd_code_intelligence
import aicd_collaboration
import aicd_identity
import aicd_integrations_github
import aicd_orchestration
import aicd_platform
import aicd_projects
import aicd_standards
import aicd_verification
from tests.support import db as testdb

MODULES = {
    "platform": aicd_platform,
    "identity": aicd_identity,
    "projects": aicd_projects,
    "cli_adapters": aicd_cli_adapters,
    "orchestration": aicd_orchestration,
    "verification": aicd_verification,
    "standards": aicd_standards,
    "collaboration": aicd_collaboration,
    "code_intelligence": aicd_code_intelligence,
    "integrations_github": aicd_integrations_github,
}
ROOT = Path(__file__).resolve().parents[2]


def test_ownership_is_disjoint() -> None:
    seen: dict[str, str] = {}
    for name, module in MODULES.items():
        for table in module.OWNED_TABLES:
            assert table not in seen, f"{table}: {seen[table]} и {name}"
            seen[table] = name


@pytest.mark.db
def test_every_table_has_owner(database: testdb.TestDatabase) -> None:
    owned = set().union(*(m.OWNED_TABLES for m in MODULES.values()))
    with psycopg.connect(database.superuser_url) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'aicd' AND c.relkind IN ('r','p') AND NOT c.relispartition"
                " AND c.relname <> 'alembic_version'"
            ).fetchall()
        }
    assert tables == owned


def test_import_contracts() -> None:
    lint = str(Path(sys.executable).with_name("lint-imports"))
    proc = subprocess.run([lint, "--no-cache"], cwd=ROOT, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_import_linter_detects_violation(tmp_path: Path) -> None:
    """Негативный тест (DoD P1-05): заведомо нарушенный контракт обязан провалить проверку."""
    cfg = tmp_path / "violation.toml"
    cfg.write_text(
        "[tool.importlinter]\n"
        'root_packages = ["aicd_api", "aicd_identity"]\n'
        "[[tool.importlinter.contracts]]\n"
        'name = "api не должен знать про identity"\n'
        'type = "forbidden"\n'
        'source_modules = ["aicd_api"]\n'
        'forbidden_modules = ["aicd_identity"]\n'
    )
    lint = str(Path(sys.executable).with_name("lint-imports"))
    proc = subprocess.run(
        [lint, "--no-cache", "--config", str(cfg)], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert proc.returncode != 0 and "BROKEN" in proc.stdout.upper()
