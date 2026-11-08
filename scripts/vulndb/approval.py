"""Approval платформы `VULN_DB_UPDATE` для обновления офлайн-БД уязвимостей (миграция 0016, OQ-51 (b)).

Предмет approval — канонический JSON (видимые человеку виды БД, экосистемы OSV, реестр БД Trivy, egress-хосты),
`subject_hash` = sha256 того же канонического JSON, что у approvals оркестрации (`aicd_orchestration.approvals`).
Запрос, авторизация (однократная) и итог — SECURITY DEFINER-функции `sys_vulndb_*`, доступные только роли
`aicd_maintenance`: подключение — DSN этой роли (файл) или `psql` в контейнере сервера (`--pg-container`).
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg

from aicd_orchestration.approvals import canonical_json, subject_hash
from aicd_platform.ids import uuid7
from scripts.backup.docker import PgTarget, psql_vars

OSV_HOST = "osv-vulnerabilities.storage.googleapis.com"
_VAR = re.compile(r":'([a-z_]+)'")


def build_subject(ecosystems: list[str], trivy_repository: str | None) -> dict[str, Any]:
    kinds = sorted({"osv"} if ecosystems else set()) + (["trivy"] if trivy_repository else [])
    hosts: set[str] = {OSV_HOST} if ecosystems else set()
    if trivy_repository:
        hosts.add(trivy_repository.split("/", 1)[0])
    parts = [f"osv ({', '.join(sorted(ecosystems))})"] if ecosystems else []
    if trivy_repository:
        parts.append(f"trivy ({trivy_repository})")
    return {
        "kinds": kinds,
        "osv_ecosystems": sorted(ecosystems),
        "trivy_db_repository": trivy_repository,
        "egress_hosts": sorted(hosts),
        "summary": "Обновление офлайн-БД уязвимостей: " + ", ".join(parts),
    }


@dataclass(frozen=True)
class PlatformDb:
    """Вызовы `sys_vulndb_*` под ролью aicd_maintenance: psycopg по DSN или psql в контейнере."""

    dsn: str | None = None
    target: PgTarget | None = None

    def scalar(self, sql: str, params: dict[str, str]) -> str:
        if self.target is not None:
            return psql_vars(self.target, sql, params)
        if self.dsn is None:
            raise ValueError("нужен DSN или PgTarget")
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            row = conn.execute(_VAR.sub(r"%(\1)s", sql), params).fetchone()
        return (
            "" if row is None or row[0] is None else str(row[0] if not isinstance(row[0], dict) else json.dumps(row[0]))
        )


REQUEST_SQL = (
    "SELECT aicd.sys_vulndb_request_approval(:'approval_id'::uuid, :'event_id'::uuid, :'audit_id'::uuid,"
    " :'workspace_id'::uuid, :'project_id'::uuid, :'email', :'subject'::jsonb, :'subject_hash');"
)
AUTHORIZE_SQL = (
    "SELECT aicd.sys_vulndb_authorize(:'approval_id'::uuid, :'audit_id'::uuid, :'subject'::jsonb,"
    " :'subject_hash', :'operator');"
)
REPORT_SQL = "SELECT aicd.sys_vulndb_report(:'approval_id'::uuid, :'audit_id'::uuid, :'outcome', :'metadata'::jsonb);"


def request(db: PlatformDb, *, workspace_id: uuid.UUID, project_id: uuid.UUID, requested_by_email: str,
            subject: dict[str, Any]) -> uuid.UUID:  # fmt: skip
    value = db.scalar(
        REQUEST_SQL,
        {
            "approval_id": str(uuid7()),
            "event_id": str(uuid7()),
            "audit_id": str(uuid7()),
            "workspace_id": str(workspace_id),
            "project_id": str(project_id),
            "email": requested_by_email,
            "subject": canonical_json(subject),
            "subject_hash": subject_hash(subject),
        },
    )
    return uuid.UUID(value)


def authorize(db: PlatformDb, approval_id: uuid.UUID, subject: dict[str, Any], operator: str) -> dict[str, Any]:
    """Однократная авторизация: APPROVED, не истёк, предмет совпадает; иначе — ошибка БД (42501/22023)."""
    value = db.scalar(
        AUTHORIZE_SQL,
        {
            "approval_id": str(approval_id),
            "audit_id": str(uuid7()),
            "subject": canonical_json(subject),
            "subject_hash": subject_hash(subject),
            "operator": operator[:200],
        },
    )
    result: dict[str, Any] = json.loads(value)
    return result


def report(db: PlatformDb, approval_id: uuid.UUID, outcome: str, metadata: dict[str, Any]) -> None:
    db.scalar(
        REPORT_SQL,
        {
            "approval_id": str(approval_id),
            "audit_id": str(uuid7()),
            "outcome": outcome,
            "metadata": canonical_json(metadata),
        },
    )
