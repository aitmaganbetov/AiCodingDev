"""Тестовая БД: роли, отдельная база на сессию pytest, миграции Alembic."""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_DIR = ROOT / "infrastructure" / "postgres"
ALEMBIC_INI = ROOT / "infrastructure" / "migrations" / "alembic.ini"

ROLE_PASSWORDS = {
    "migrator_password": "test-migrator-pw",
    "app_password": "test-app-pw",
    "worker_password": "test-worker-pw",
    "maintenance_password": "test-maintenance-pw",
}


def admin_url() -> str | None:
    return os.environ.get("AICD_TEST_DATABASE_ADMIN_URL")


def _render(script: str, variables: dict[str, str]) -> list[str]:
    """Выполняет подстановку переменных psql (:'name') и делит скрипт на операторы."""
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith("\\") and not ln.strip().startswith("--")]
    body = "\n".join(lines)
    for name, value in variables.items():
        body = body.replace(f":'{name}'", sql.Literal(value).as_string())
    if re.search(r":'[a-z_]+'", body):
        raise ValueError("не все переменные psql подставлены")
    return [stmt.strip() for stmt in body.split(";") if stmt.strip()]


def _with_db(url: str, dbname: str) -> str:
    return re.sub(r"/[^/?]*(\?|$)", f"/{dbname}\\1", url, count=1) if url.count("/") >= 3 else url


def ensure_roles(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'aicd_migrator'").fetchone()
        if exists:
            for role, var in (
                ("aicd_migrator", "migrator_password"),
                ("aicd_app", "app_password"),
                ("aicd_worker", "worker_password"),
                ("aicd_maintenance", "maintenance_password"),
            ):
                conn.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(ROLE_PASSWORDS[var]))
                )
            return
        for stmt in _render((BOOTSTRAP_DIR / "bootstrap-roles.sql").read_text(), ROLE_PASSWORDS):
            conn.execute(stmt)


@dataclass(frozen=True)
class TestDatabase:
    __test__ = False  # не тестовый класс pytest

    name: str
    admin_url: str

    def url_for(self, role: str) -> str:
        password = {
            "aicd_migrator": ROLE_PASSWORDS["migrator_password"],
            "aicd_app": ROLE_PASSWORDS["app_password"],
            "aicd_worker": ROLE_PASSWORDS["worker_password"],
            "aicd_maintenance": ROLE_PASSWORDS["maintenance_password"],
        }[role]
        base = _with_db(self.admin_url, self.name)
        return re.sub(r"//[^@]+@", f"//{role}:{password}@", base, count=1)

    @property
    def superuser_url(self) -> str:
        return _with_db(self.admin_url, self.name)


def create_database(url: str, prefix: str = "aicd_test") -> TestDatabase:
    ensure_roles(url)
    name = f"{prefix}_{secrets.token_hex(4)}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    db = TestDatabase(name=name, admin_url=url)
    with psycopg.connect(db.superuser_url, autocommit=True) as conn:
        for stmt in _render((BOOTSTRAP_DIR / "bootstrap-database.sql").read_text(), {}):
            conn.execute(stmt)
    return db


def drop_database(db: TestDatabase) -> None:
    with psycopg.connect(db.admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db.name)))


def alembic(db: TestDatabase, action: str, revision: str) -> None:
    os.environ["AICD_MIGRATOR_DATABASE_URL"] = db.url_for("aicd_migrator")
    cfg = Config(str(ALEMBIC_INI))
    if action == "upgrade":
        command.upgrade(cfg, revision)
    elif action == "downgrade":
        command.downgrade(cfg, revision)
    else:
        raise ValueError(action)
