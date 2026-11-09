"""Фабрики строк качества/evidence (миграции 0003, 0008) для тестов БД.

Работают через любое соединение с `search_path = aicd` (суперпользователь — без RLS; под ролями
приложения — в контексте workspace).
"""

from __future__ import annotations

import secrets
import uuid

import psycopg

from aicd_platform.ids import uuid7
from tests.support.orchestration import STRICT_PROFILE_ID, Tenant

TERMINAL = ("PASS", "FAIL", "ERROR", "SKIPPED", "NOT_APPLICABLE")


def sha1() -> str:
    return secrets.token_hex(20)


def sha256() -> str:
    return secrets.token_hex(32)


def tenant_profile(conn: psycopg.Connection, tenant: Tenant) -> uuid.UUID:
    """Профиль strict workspace (M-таблица): gates теста не попадают в глобальные профили других тестов."""
    pid = uuid7()
    conn.execute(
        "INSERT INTO quality_profiles (id, workspace_id, code, version, definition) VALUES (%s, %s, 'strict', 1, '{}')",
        (pid, tenant.workspace_id),
    )
    return pid


def ensure_gate(
    conn: psycopg.Connection, name: str, gate_type: str, *, profile: tuple[uuid.UUID, uuid.UUID] | None = None
) -> uuid.UUID:
    """Определение gate: глобального профиля strict v1 (seed 0004) или `profile = (profile_id, workspace_id)`."""
    profile_id, ws = profile or (STRICT_PROFILE_ID, None)
    row = conn.execute(
        "SELECT id FROM quality_gates WHERE profile_id = %s AND name = %s", (profile_id, name)
    ).fetchone()
    if row is not None:
        gid: uuid.UUID = row[0]
        return gid
    if profile is None:
        raise LookupError(f"gate {name} отсутствует в seed глобального профиля strict")
    gid = uuid7()
    conn.execute(
        "INSERT INTO quality_gates (id, workspace_id, profile_id, name, gate_type) VALUES (%s, %s, %s, %s, %s)",
        (gid, ws, profile_id, name, gate_type),
    )
    return gid


def artifact(conn: psycopg.Connection, tenant: Tenant, run: uuid.UUID | None, kind: str = "report") -> uuid.UUID:
    art = uuid7()
    conn.execute(
        "INSERT INTO artifacts (id, workspace_id, project_id, run_id, kind, storage_ref, sha256, size_bytes, mime_type)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 'text/plain')",
        (
            art,
            tenant.workspace_id,
            tenant.project_id,
            run,
            kind,
            f"file://artifacts/{tenant.workspace_id}/{art}",
            sha256(),
        ),
    )
    return art


def evidence(
    conn: psycopg.Connection,
    tenant: Tenant,
    run: uuid.UUID,
    commit: str,
    *,
    status: str = "VERIFIED",
    producer: str = "verification_engine",
    user: uuid.UUID | None = None,
    kind: str = "command_result",
) -> uuid.UUID:
    ev = uuid7()
    process = kind in ("command_result", "test_report", "scan_report", "benchmark")
    conn.execute(
        "INSERT INTO evidence (id, workspace_id, run_id, kind, artifact_id, producer, producer_user_id,"
        " verification_status, commit_sha, command, exit_code, started_at, finished_at, verified_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            ev,
            tenant.workspace_id,
            run,
            kind,
            artifact(conn, tenant, run),
            producer,
            user,
            status,
            commit if process else None,
            "tool --check" if process else None,
            0 if process else None,
            "now" if process else None,
            "now" if process else None,
            "now" if status in ("VERIFIED", "REJECTED") else None,
        ),
    )
    return ev


def gate_execution(
    conn: psycopg.Connection,
    tenant: Tenant,
    run: uuid.UUID,
    gate_id: uuid.UUID,
    gate_type: str,
    status: str,
    commit: str,
    *,
    evidence_id: uuid.UUID | None = None,
    task: uuid.UUID | None = None,
    reason: str | None = None,
    error_code: str | None = None,
) -> uuid.UUID:
    ge = uuid7()
    if reason is None and status in ("NOT_APPLICABLE", "SKIPPED"):
        reason = "no markers"
    conn.execute(
        "INSERT INTO gate_executions (id, workspace_id, run_id, task_id, gate_id, gate_type, required, status,"
        " commit_sha, evidence_id, not_applicable_reason, error_code, finished_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, true, %s, %s, %s, %s, %s, %s)",
        (
            ge,
            tenant.workspace_id,
            run,
            task,
            gate_id,
            gate_type,
            status,
            commit,
            evidence_id,
            reason,
            error_code,
            "now" if status in TERMINAL else None,
        ),
    )
    return ge


def finding(
    conn: psycopg.Connection,
    tenant: Tenant,
    run: uuid.UUID,
    *,
    severity: str = "high",
    gate: tuple[uuid.UUID, uuid.UUID] | None = None,  # (gate_execution_id, gate_id)
    evidence_id: uuid.UUID | None = None,
    cwe: str | None = None,
    rule: str = "rule.x",
) -> uuid.UUID:
    fid = uuid7()
    conn.execute(
        "INSERT INTO findings (id, workspace_id, run_id, source, source_gate_id, gate_execution_id, severity, rule_id,"
        " cwe_id, title, file_path, line, fingerprint, evidence_id)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'finding', 'src/app.py', 3, %s, %s)",
        (
            fid,
            tenant.workspace_id,
            run,
            "gate" if gate else "agent_review",
            gate[1] if gate else None,
            gate[0] if gate else None,
            severity,
            rule,
            cwe,
            sha256(),
            evidence_id,
        ),
    )
    return fid
