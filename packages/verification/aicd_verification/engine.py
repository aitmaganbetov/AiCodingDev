"""Verification Engine v0 (P3-14; QUALITY_GATES §4–§5, DATA_FLOW §10, ТЗ §14, AC-11/AC-14).

Реализует `VerifierPort.verify_task` оркестратора. Для каждого gate профиля run:

1. строка `gate_executions` (PENDING → RUNNING → PASS/FAIL/ERROR/SKIPPED/NOT_APPLICABLE), `required` —
   snapshot из `quality_gates` (SCOPE обязателен всегда); прежняя актуальная строка помечается
   `superseded_at` (новый commit / повтор после ERROR), терминальный результат на том же SHA
   переиспользуется (повторная доставка не создаёт второй PASS, FP-18);
2. процесс gate — фиксированный argv без shell в verification-контейнере (`DockerSandboxRunner`:
   network none, worktree read-only в /workspace, tmpfs, без CLI credentials) либо `LocalProcessRunner`
   (тесты/локальный fallback); сначала probe инструмента — нет инструмента → ERROR (TOOL_UNAVAILABLE);
3. полный вывод (с редактированием секретов) → artifact (sha256, повторное чтение со сверкой хеша),
   evidence (`commit_sha`, `command`, `exit_code`, `environment`, `started_at`, `finished_at`,
   `verification_status=VERIFIED` только когда результат процесса сохранён и перечитан);
4. одна транзакция: artifacts + evidence + терминальный статус + outbox `gate.completed`.

PASS — только exit code 0, согласованный с отчётом, и VERIFIED evidence (триггер БД это дублирует).
`passed` = все обязательные gates PASS/NOT_APPLICABLE и хотя бы один PASS.

Phase 6 (P6-02…P6-08): набор gates — реестр провайдеров (`registry.GATE_SPECS`); план задачи —
`profiles.plan_gates` (профиль run × строки `quality_gates` × применимость по commit и
`project_technologies`); обязательность — эффективная по плану (advisory gates не блокируют).
Вердикт gate — `GateSpec.evaluate` с нормализованными findings, которые в той же транзакции
сохраняются в `findings` (upsert по отпечатку; исчезнувшие на новом commit → VERIFIED_FIXED с
evidence повторного прогона), `test_results` и `evidence_links` (produced_by/verifies/supports).
Деструктивная миграция: PASS только при APPROVED approval DESTRUCTIVE_MIGRATION (порт `approvals`).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import platform
import shlex
import shutil
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, get_args

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_platform import outbox
from aicd_platform.artifacts import ArtifactIntegrityError, ArtifactNotFoundError, ArtifactStore
from aicd_platform.clock import utcnow
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from aicd_platform.redaction import redact_text
from aicd_security.docker_runner import DockerError, DockerSandboxRunner
from aicd_security.git import GitRepositoryError, run_git
from aicd_security.sandbox import ContainerSpec, Mount, ResourceLimits, SandboxProcess, SandboxRunner
from aicd_verification.findings import Finding, dedupe, severity_counts
from aicd_verification.gates import (
    CONTAINER_INPUT_DIR,
    CONTAINER_WORKDIR,
    Applicability,
    ErrorCode,
    GateContext,
    GateSpec,
    RepoFacts,
    StackInfo,
    Verdict,
    changed_python_files,
    collect_repo_facts,
    write_scope_input,
)
from aicd_verification.images import image_key_for
from aicd_verification.migrations import APPROVAL_ACTION, ApprovalsPort, approval_state
from aicd_verification.profiles import GateRow, plan_gates, tool_missing_effect
from aicd_verification.registry import GATE_SPECS, detect_all
from aicd_verification.security_gates import SECURITY_GATES

log = logging.getLogger("aicd.verification")

DEFAULT_IMAGE = "python:3.12-slim-bookworm"
PROBE_TIMEOUT_SECONDS = 60
TERMINAL = frozenset({"PASS", "FAIL", "ERROR", "SKIPPED", "NOT_APPLICABLE"})
REUSABLE = frozenset({"PASS", "FAIL", "NOT_APPLICABLE"})  # детерминированный результат на том же SHA
OK_FOR_COMPLETION = frozenset({"PASS", "NOT_APPLICABLE"})
# Коды, допустимые CHECK gate_executions.error_code (0008); остальные — только в error_message.
# Коды ошибок gate = CHECK ck_gate_executions_error_code (0008); единый источник — gates.ErrorCode.
DB_ERROR_CODES = frozenset(get_args(ErrorCode))
MAX_FINDINGS_PER_GATE = 500


@dataclass(frozen=True)
class VerificationOutcome:
    """Результат verify_task. Структурно совпадает с `aicd_orchestration.engine.VerificationOutcome`:
    orchestration — слой выше verification, поэтому порт реализуется структурно (без импорта)."""

    passed: bool
    gate_execution_ids: tuple[uuid.UUID, ...] = ()
    summary: str = ""


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None  # None — процесс не запустился
    stdout: bytes
    stderr: bytes
    started_at: datetime
    finished_at: datetime
    timed_out: bool = False
    truncated: bool = False
    start_error: str | None = None
    image_id: str | None = None


@dataclass
class _GateRun:
    gate_id: uuid.UUID
    name: str
    gate_type: str
    required: bool
    timeout_seconds: int
    spec: GateSpec | None
    applicability: Applicability | None
    execution_id: uuid.UUID | None = None
    status: str | None = None
    reused: bool = False
    detail: str = ""
    config: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class _TaskContext:
    workspace_id: uuid.UUID
    run_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    commit_sha: str
    worktree: Path
    staging: Path | None = None
    precondition_error: tuple[ErrorCode, str] | None = None
    dirty: bool = False
    facts: RepoFacts | None = None
    base_sha: str | None = None
    profile: str = "strict"
    stack: StackInfo = field(default_factory=StackInfo)
    extra: dict[str, Any] = field(default_factory=dict)


class VerificationEngine:
    """Структурная реализация `VerifierPort` оркестратора."""

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        store: ArtifactStore,
        runner: SandboxRunner,
        image: str = DEFAULT_IMAGE,
        images: Mapping[str, str] | None = None,
        staging_root: Path | None = None,
        local_env: Mapping[str, str] | None = None,
        max_parallel_gates: int = 2,
        timeout_cap_seconds: int | None = None,
        max_output_bytes: int = 8 * 1024 * 1024,
        limits: ResourceLimits | None = None,
        approvals: ApprovalsPort | None = None,
    ) -> None:
        self._engine = engine
        self._store = store
        self._runner = runner
        self._image = image
        self._images = dict(images or {})
        self._staging_root = staging_root
        self._is_docker = isinstance(runner, DockerSandboxRunner)
        self._local_env = dict(local_env or {"PATH": os.defpath})
        self._semaphore = asyncio.Semaphore(max(1, max_parallel_gates))
        self._timeout_cap = timeout_cap_seconds
        self._max_output = max_output_bytes
        self._limits = limits or ResourceLimits(cpus=2.0, memory_mb=2048, pids=256, timeout_seconds=600)
        self._approvals = approvals

    @staticmethod
    def _ctx(ws: uuid.UUID) -> DbContext:
        return DbContext(workspace_id=ws, actor_type="system")

    # ---------------------------------------------------------------- VerifierPort
    async def verify_task(
        self,
        *,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        task_id: uuid.UUID,
        worktree_path: Any,
        commit_sha: str,
        quality_profile: str,
    ) -> VerificationOutcome:
        async with transaction(self._engine, self._ctx(workspace_id)) as conn:
            run = (
                (
                    await conn.execute(
                        text(
                            "SELECT project_id, quality_profile, quality_profile_id, base_commit_sha FROM runs WHERE id = :id"
                        ),
                        {"id": run_id},
                    )
                )
                .mappings()
                .first()
            )
            if run is None:
                return self._outcome(False, [], "run not found")
            if run["quality_profile"] != quality_profile:
                return self._outcome(False, [], "quality profile mismatch with run snapshot")
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, name, gate_type, required, timeout_seconds, config FROM quality_gates"
                            " WHERE profile_id = :p ORDER BY name"
                        ),
                        {"p": run["quality_profile_id"]},
                    )
                )
                .mappings()
                .all()
            )
            technologies = (
                (
                    await conn.execute(
                        text(
                            "SELECT t.ecosystem, t.name FROM project_technologies pt"
                            " JOIN technology_versions tv ON tv.id = pt.technology_version_id"
                            " JOIN technologies t ON t.id = tv.technology_id WHERE pt.project_id = :p"
                        ),
                        {"p": run["project_id"]},
                    )
                )
                .mappings()
                .all()
            )
        if not any(r["gate_type"] == "SCOPE" for r in rows):
            # Fail-closed: без определения SCOPE задачу нельзя признать проверенной (ТЗ §14).
            return self._outcome(False, [], f"profile {quality_profile} has no SCOPE gate definition")

        task = _TaskContext(
            workspace_id,
            run_id,
            task_id,
            run["project_id"],
            commit_sha.lower(),
            Path(str(worktree_path)),
            base_sha=run["base_commit_sha"],
            profile=quality_profile,
            stack=StackInfo.from_rows(technologies),
        )
        await asyncio.to_thread(self._prepare, task)
        applicability = detect_all(task.facts, task.stack) if task.facts is not None else {}
        plan = plan_gates(
            quality_profile,
            [
                GateRow(
                    r["id"],
                    r["name"],
                    r["gate_type"],
                    bool(r["required"]),
                    int(r["timeout_seconds"]),
                    dict(r["config"] or {}),
                )
                for r in rows
            ],
            applicability,
            known_gates=GATE_SPECS,
        )
        gates = [
            _GateRun(
                p.row.gate_id,
                p.row.name,
                p.row.gate_type,
                p.required,
                self._timeout(p.row.timeout_seconds),
                GATE_SPECS.get(p.row.name),
                # План (а не сырой детектор): «-» профиля и неприменимость дают NOT_APPLICABLE с причиной.
                Applicability(p.disposition == "run", p.reason, p.markers),
                config=p.row.config,
            )
            for p in plan.gates
        ]
        try:
            for gate in gates:
                await self._open_execution(task, gate)
            pending = [g for g in gates if not g.reused]
            await asyncio.gather(*(self._run_gate(task, g) for g in pending))
        finally:
            if task.staging is not None:
                shutil.rmtree(task.staging, ignore_errors=True)
        return self._verdict(gates)

    # ---------------------------------------------------------------- подготовка (хост, по git-объектам)
    def _timeout(self, configured: int) -> int:
        return min(configured, self._timeout_cap) if self._timeout_cap else configured

    def _prepare(self, task: _TaskContext) -> None:
        try:
            head = run_git(task.worktree, "rev-parse", "HEAD")
            run_git(task.worktree, "cat-file", "-e", f"{task.commit_sha}^{{commit}}")
        except (GitRepositoryError, OSError, ValueError):
            task.precondition_error = ("SHA_MISMATCH", "worktree or commit is not readable")
            return
        if head.lower() != task.commit_sha:
            task.precondition_error = ("SHA_MISMATCH", "worktree HEAD does not match the verified commit")
            return
        try:
            task.dirty = bool(run_git(task.worktree, "status", "--porcelain", "--untracked-files=normal"))
            task.facts = collect_repo_facts(task.worktree, task.commit_sha, task.base_sha)
        except (GitRepositoryError, OSError, ValueError) as exc:
            task.precondition_error = ("INFRA", f"repository inspection failed: {type(exc).__name__}")
            return
        if task.base_sha is not None:
            if self._staging_root is not None:
                self._staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix="aicd-verify-", dir=self._staging_root))
            staging.chmod(0o755)  # контейнер работает не от root: каталог входа должен читаться
            task.staging = staging
            try:
                write_scope_input(task.worktree, task.commit_sha, task.base_sha, task.facts, staging)
                for item in staging.iterdir():
                    item.chmod(0o644)
            except (GitRepositoryError, OSError, ValueError) as exc:
                task.precondition_error = ("INFRA", f"scope input preparation failed: {type(exc).__name__}")

    # ---------------------------------------------------------------- строки gate_executions
    async def _open_execution(self, task: _TaskContext, gate: _GateRun) -> None:
        async with transaction(self._engine, self._ctx(task.workspace_id)) as conn:
            current = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, status, commit_sha, error_message FROM gate_executions WHERE run_id = :r AND task_id = :t"
                            " AND gate_id = :g AND superseded_at IS NULL FOR UPDATE"
                        ),
                        {"r": task.run_id, "t": task.task_id, "g": gate.gate_id},
                    )
                )
                .mappings()
                .first()
            )
            if current is not None:
                awaiting_approval = str(current["error_message"] or "").startswith("APPROVAL_REQUIRED")
                if current["commit_sha"] == task.commit_sha and current["status"] in REUSABLE and not awaiting_approval:
                    gate.execution_id, gate.status, gate.reused = current["id"], current["status"], True
                    return
                if current["status"] in TERMINAL:
                    await conn.execute(
                        text("UPDATE gate_executions SET superseded_at = now() WHERE id = :id"), {"id": current["id"]}
                    )
                else:  # зависшая PENDING/RUNNING строка прошлого прогона
                    await conn.execute(
                        text(
                            "UPDATE gate_executions SET status = 'ERROR', finished_at = now(), superseded_at = now(),"
                            " error_message = 'INFRA: stale execution superseded' WHERE id = :id"
                        ),
                        {"id": current["id"]},
                    )
            attempt = (
                await conn.execute(
                    text(
                        "SELECT coalesce(max(attempt_no), 0) + 1 FROM gate_executions"
                        " WHERE run_id = :r AND task_id = :t AND gate_id = :g"
                    ),
                    {"r": task.run_id, "t": task.task_id, "g": gate.gate_id},
                )
            ).scalar_one()
            gate.execution_id = uuid7()
            await conn.execute(
                text(
                    "INSERT INTO gate_executions (id, workspace_id, run_id, task_id, gate_id, gate_type, required,"
                    " status, attempt_no, commit_sha) VALUES (:id, :ws, :r, :t, :g, :type, :req, 'PENDING', :n, :sha)"
                ),
                {
                    "id": gate.execution_id,
                    "ws": task.workspace_id,
                    "r": task.run_id,
                    "t": task.task_id,
                    "g": gate.gate_id,
                    "type": gate.gate_type,
                    "req": gate.required,
                    "n": attempt,
                    "sha": task.commit_sha,
                },
            )

    async def _set_running(self, task: _TaskContext, gate: _GateRun) -> None:
        async with transaction(self._engine, self._ctx(task.workspace_id)) as conn:
            await conn.execute(
                text("UPDATE gate_executions SET status = 'RUNNING', started_at = now() WHERE id = :id"),
                {"id": gate.execution_id},
            )

    async def _finish(
        self,
        task: _TaskContext,
        gate: _GateRun,
        *,
        status: str,
        error: str | None = None,
        reason: str | None = None,
        evidence: dict[str, Any] | None = None,
        verdict: Verdict | None = None,
        duration_ms: int = 0,
    ) -> None:
        assert gate.execution_id is not None  # noqa: S101 — инвариант: строка создана _open_execution
        evidence_id: uuid.UUID | None = None
        # Находки сохраняются только для разобранного результата процесса (PASS/FAIL): вывод упавшего
        # инструмента (ERROR) не является надёжным отчётом.
        findings = dedupe(verdict.findings)[:MAX_FINDINGS_PER_GATE] if verdict and status in ("PASS", "FAIL") else []
        code = error.split(":", 1)[0].strip() if error and ":" in error else None
        async with transaction(self._engine, self._ctx(task.workspace_id)) as conn:
            if evidence is not None:
                evidence_id = await self._insert_evidence(conn, task, evidence)
            await conn.execute(
                text(
                    "UPDATE gate_executions SET status = :s, evidence_id = :e, error_message = :err, error_code = :code,"
                    " not_applicable_reason = :na, started_at = coalesce(started_at, now()), finished_at = now()"
                    " WHERE id = :id"
                ),
                {
                    "s": status,
                    "e": evidence_id,
                    "err": error[:4000] if error else None,
                    "code": code if code in DB_ERROR_CODES and status in ("ERROR", "FAIL") else None,
                    "na": reason[:2000] if reason else None,
                    "id": gate.execution_id,
                },
            )
            if evidence_id is not None and evidence is not None:
                await self._link(conn, task, evidence_id, "gate_execution", gate.execution_id, "produced_by")
                await self._link(conn, task, evidence_id, "task", task.task_id, "verifies")
                if verdict is not None and status in ("PASS", "FAIL"):
                    await self._insert_test_results(conn, task, gate, verdict, evidence, duration_ms)
                    await self._persist_findings(conn, task, gate, findings, evidence_id)
            await outbox.emit(
                conn,
                event_type="gate.completed",
                aggregate_type="gate_execution",
                aggregate_id=gate.execution_id,
                workspace_id=task.workspace_id,
                run_id=task.run_id,
                payload={
                    "gate_execution_id": str(gate.execution_id),
                    "gate_type": gate.gate_type,
                    "task_id": str(task.task_id),
                    "status": status,
                    "required": gate.required,
                    "commit_sha": task.commit_sha,
                    "evidence_id": str(evidence_id) if evidence_id else None,
                    "findings": severity_counts(findings),
                    "duration_ms": max(0, duration_ms),
                    "tool": (gate.spec.tool if gate.spec else gate.name)[:100],
                },
            )
        gate.status = status
        gate.detail = error or reason or ""

    @staticmethod
    async def _link(
        conn: AsyncConnection,
        task: _TaskContext,
        evidence_id: uuid.UUID,
        target_type: str,
        target_id: uuid.UUID,
        relation: str,
    ) -> None:
        await conn.execute(
            text(
                "INSERT INTO evidence_links (id, workspace_id, evidence_id, target_type, target_id, relation)"
                " VALUES (:id, :ws, :e, :tt, :tid, :rel) ON CONFLICT ON CONSTRAINT uq_evidence_links DO NOTHING"
            ),
            {
                "id": uuid7(),
                "ws": task.workspace_id,
                "e": evidence_id,
                "tt": target_type,
                "tid": target_id,
                "rel": relation,
            },
        )

    @staticmethod
    async def _insert_test_results(
        conn: AsyncConnection,
        task: _TaskContext,
        gate: _GateRun,
        verdict: Verdict,
        evidence: dict[str, Any],
        duration_ms: int,
    ) -> None:
        if verdict.tests is None and verdict.coverage_percent is None:
            return
        tests = verdict.tests or {}
        await conn.execute(
            text(
                "INSERT INTO test_results (id, workspace_id, gate_execution_id, suite, passed, failed, skipped, errors,"
                " duration_ms, coverage_percent, report_artifact_id)"
                " VALUES (:id, :ws, :g, :suite, :p, :f, :s, :e, :d, :cov, :a)"
            ),
            {
                "id": uuid7(),
                "ws": task.workspace_id,
                "g": gate.execution_id,
                "suite": gate.name[:500],
                "p": max(0, int(tests.get("passed", 0))),
                "f": max(0, int(tests.get("failed", 0))),
                "s": max(0, int(tests.get("skipped", 0))),
                "e": max(0, int(tests.get("errors", 0))),
                "d": max(0, duration_ms),
                "cov": verdict.coverage_percent,
                "a": evidence["artifact_id"],
            },
        )

    async def _persist_findings(
        self,
        conn: AsyncConnection,
        task: _TaskContext,
        gate: _GateRun,
        findings: list[Finding],
        evidence_id: uuid.UUID,
    ) -> None:
        """Upsert по (run, fingerprint): повтор находки на новом commit переоткрывает FIXED/VERIFIED_FIXED;
        решения человека (FALSE_POSITIVE/ACCEPTED_RISK) сохраняются. Находки этого gate задачи, которых
        нет в новом результате, закрываются VERIFIED_FIXED со ссылкой на evidence повторного прогона."""
        fingerprints: list[str] = []
        for f in findings:
            finding_id = (
                await conn.execute(
                    text(
                        "INSERT INTO findings (id, workspace_id, run_id, task_id, source, source_gate_id,"
                        " gate_execution_id, severity, rule_id, cwe_id, title, file_path, line, status, fingerprint,"
                        " evidence_id) VALUES (:id, :ws, :r, :t, 'gate', :g, :ge, :sev, :rule, :cwe, :title, :path,"
                        " :line, 'OPEN', :fp, :ev)"
                        " ON CONFLICT ON CONSTRAINT uq_findings_run_fingerprint DO UPDATE SET"
                        " task_id = EXCLUDED.task_id, source_gate_id = EXCLUDED.source_gate_id,"
                        " gate_execution_id = EXCLUDED.gate_execution_id, severity = EXCLUDED.severity,"
                        " title = EXCLUDED.title, line = EXCLUDED.line, evidence_id = EXCLUDED.evidence_id,"
                        " fixed_in_commit_sha = CASE WHEN findings.status IN ('FIXED','VERIFIED_FIXED') THEN NULL"
                        "   ELSE findings.fixed_in_commit_sha END,"
                        " status = CASE WHEN findings.status IN ('FIXED','VERIFIED_FIXED') THEN 'OPEN'"
                        "   ELSE findings.status END"
                        " WHERE findings.source = 'gate' RETURNING id"
                    ),
                    {
                        "id": uuid7(),
                        "ws": task.workspace_id,
                        "r": task.run_id,
                        "t": task.task_id,
                        "g": gate.gate_id,
                        "ge": gate.execution_id,
                        "sev": f.severity,
                        "rule": f.rule_id,
                        "cwe": f.cwe_id,
                        "title": f.title,
                        "path": f.file_path,
                        "line": f.line,
                        "fp": f.fingerprint,
                        "ev": evidence_id,
                    },
                )
            ).scalar_one_or_none()
            fingerprints.append(f.fingerprint)
            if finding_id is not None:
                await self._link(conn, task, evidence_id, "finding", finding_id, "supports")
        await conn.execute(
            text(
                "UPDATE findings SET status = 'VERIFIED_FIXED', evidence_id = :ev, fixed_in_commit_sha = :sha"
                " WHERE run_id = :r AND task_id = :t AND source = 'gate' AND source_gate_id = :g"
                " AND status IN ('OPEN','ACKNOWLEDGED','FIXED') AND NOT (fingerprint = ANY(:fps))"
            ),
            {
                "ev": evidence_id,
                "sha": task.commit_sha,
                "r": task.run_id,
                "t": task.task_id,
                "g": gate.gate_id,
                "fps": fingerprints,
            },
        )

    async def _insert_evidence(self, conn: AsyncConnection, task: _TaskContext, ev: dict[str, Any]) -> uuid.UUID:
        await conn.execute(
            text(
                "INSERT INTO artifacts (id, workspace_id, project_id, run_id, kind, storage_ref, sha256, size_bytes,"
                " mime_type, is_sensitive, redaction_applied, retention_class)"
                " VALUES (:id, :ws, :p, :r, :kind, :ref, :sha, :size, 'text/plain', false, true, 'evidence')"
            ),
            {
                "id": ev["artifact_id"],
                "ws": task.workspace_id,
                "p": task.project_id,
                "r": task.run_id,
                "kind": ev["artifact_kind"],
                "ref": ev["storage_ref"],
                "sha": ev["sha256"],
                "size": ev["size"],
            },
        )
        evidence_id = uuid7()
        await conn.execute(
            text(
                "INSERT INTO evidence (id, workspace_id, run_id, task_id, kind, artifact_id, producer,"
                " verification_status, commit_sha, command, exit_code, environment, started_at, finished_at,"
                " verified_at) VALUES (:id, :ws, :r, :t, :kind, :a, 'verification_engine', :vs, :sha, :cmd, :code,"
                " CAST(:env AS jsonb), :st, :fin, :vat)"
            ),
            {
                "id": evidence_id,
                "ws": task.workspace_id,
                "r": task.run_id,
                "t": task.task_id,
                "kind": ev["evidence_kind"],
                "a": ev["artifact_id"],
                "vs": ev["verification_status"],
                "sha": task.commit_sha,
                "cmd": ev["command"][:8192],
                "code": ev["exit_code"],
                "env": json.dumps(ev["environment"]),
                "st": ev["started_at"],
                "fin": ev["finished_at"],
                "vat": utcnow() if ev["verification_status"] in ("VERIFIED", "REJECTED") else None,
            },
        )
        return evidence_id

    # ---------------------------------------------------------------- исполнение gate
    async def _run_gate(self, task: _TaskContext, gate: _GateRun) -> None:
        try:
            await self._run_gate_inner(task, gate)
        except Exception as exc:
            log.exception("verification gate %s failed unexpectedly", gate.name)
            if gate.status is None:
                with contextlib.suppress(Exception):
                    await self._finish(task, gate, status="ERROR", error=f"INFRA: {type(exc).__name__}")

    async def _run_gate_inner(self, task: _TaskContext, gate: _GateRun) -> None:
        spec = gate.spec
        if spec is None:
            if gate.required:
                await self._finish(
                    task, gate, status="ERROR", error=f"UNSUPPORTED_GATE: no v0 runner for gate '{gate.name}'"
                )
            else:
                await self._finish(task, gate, status="SKIPPED", reason=f"no v0 runner for gate '{gate.name}'")
            return
        if task.precondition_error is not None:
            code, message = task.precondition_error
            await self._finish(task, gate, status="ERROR", error=f"{code}: {message}")
            return
        if spec.needs_scope_input and task.base_sha is None:
            await self._finish(task, gate, status="ERROR", error="NO_BASE_COMMIT: run has no base commit")
            return
        if gate.applicability is None or not gate.applicability.applicable:
            reason = gate.applicability.reason if gate.applicability else "applicability unknown"
            markers = ", ".join(gate.applicability.markers) if gate.applicability else ""
            await self._finish(
                task, gate, status="NOT_APPLICABLE", reason=f"detector: {reason}; checked markers: {markers}"
            )
            return
        if spec.executes_repository and task.dirty:
            await self._finish(
                task,
                gate,
                status="ERROR",
                error="WORKTREE_DIRTY: worktree has uncommitted changes, result would not match commit",
            )
            return

        async with self._semaphore:
            await self._set_running(task, gate)
            ctx = self._gate_context(task, gate)
            security = SECURITY_GATES.get(spec.name)
            if security is not None:
                if security.db is not None:
                    # Офлайн-БД уязвимостей подключается отдельным утверждённым и аудируемым шагом обновления;
                    # пока она не смонтирована, gate не может дать честный вердикт (ERROR ≠ PASS).
                    await self._finish(
                        task,
                        gate,
                        status="ERROR",
                        error=f"TOOL_UNAVAILABLE: offline {security.db} vulnerability DB is not configured",
                    )
                    return
                if task.staging is not None:  # файлы платформы (правила/конфиги) для /aicd-input (ro)
                    for name, content in security.input_files(ctx).items():
                        target = task.staging / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(content)
                        target.chmod(0o644)
            probe = await self._execute(task, spec, spec.probe(ctx), PROBE_TIMEOUT_SECONDS, gate)
            if probe.exit_code != 0 or probe.timed_out:
                effect = tool_missing_effect(task.profile, gate.gate_type)
                await self._finish(
                    task,
                    gate,
                    status="ERROR",
                    error=f"TOOL_UNAVAILABLE: '{spec.tool}' is not available in the verification "
                    f"environment ({probe.start_error or f'probe exit {probe.exit_code}'});"
                    f" profile {task.profile}: {effect}",
                )
                return
            tool_version = redact_text(
                probe.stdout.decode("utf-8", "replace").strip().splitlines()[0][:200]
                if probe.stdout.strip()
                else probe.stderr.decode("utf-8", "replace")[:200]
            )
            started = time.monotonic()
            result = await self._execute(task, spec, spec.build_argv(ctx), gate.timeout_seconds, gate)
            duration_ms = int((time.monotonic() - started) * 1000)

        stdout = result.stdout.decode("utf-8", "replace")
        if result.exit_code is None:
            await self._finish(
                task,
                gate,
                status="ERROR",
                error=f"INFRA: gate process did not start ({result.start_error})",
                duration_ms=duration_ms,
            )
            return
        if result.timed_out:
            verdict = Verdict("ERROR", "TIMEOUT", f"gate exceeded {gate.timeout_seconds}s")
        elif result.exit_code < 0:
            verdict = Verdict("ERROR", "INFRA", f"gate process killed by signal {-result.exit_code}")
        else:
            try:
                verdict = spec.run_evaluate(result.exit_code, stdout, ctx)
            except Exception as exc:  # noqa: BLE001 — сбой разбора отчёта ≠ PASS
                verdict = Verdict("ERROR", "REPORT_UNPARSABLE", f"report interpretation failed: {type(exc).__name__}")
        if result.truncated and verdict.status == "PASS":
            verdict = Verdict("ERROR", "REPORT_UNPARSABLE", "gate output exceeded the capture limit")
        if verdict.approval is not None and verdict.status == "PASS":
            verdict = await self._approval_verdict(task, gate, verdict)

        display = shlex.join(spec.display_argv(ctx))
        environment = self._environment(spec, tool_version, result)
        stored = await self._store_output(task, gate, display, result, environment)
        evidence_status = stored["verification_status"]
        status = verdict.status
        error = f"{verdict.error_code}: {verdict.message}" if verdict.error_code else (verdict.message or None)
        if evidence_status != "VERIFIED" and status in ("PASS", "FAIL"):
            # Без сохранённого и перечитанного результата процесса вердикта нет (ТЗ §14, FP-14).
            status, error = "ERROR", "INFRA: gate output could not be stored and re-verified"
        await self._finish(
            task,
            gate,
            status=status,
            error=error if status in ("ERROR", "FAIL") else None,
            evidence=stored,
            verdict=verdict,
            duration_ms=duration_ms,
        )

    async def _approval_verdict(self, task: _TaskContext, gate: _GateRun, verdict: Verdict) -> Verdict:
        """PASS gate с предметом approval (деструктивная миграция) — только при APPROVED решении."""
        assert verdict.approval is not None  # noqa: S101 — вызывается только с предметом
        subject = {**verdict.approval, "project_id": str(task.project_id)}
        async with transaction(self._engine, self._ctx(task.workspace_id)) as conn:
            state = await approval_state(conn, project_id=task.project_id, action_type=APPROVAL_ACTION, subject=subject)
            if state.status == "APPROVED":
                accepted = tuple(
                    replace(f, severity="info", title=f"approved ({state.approval_id}): {f.title}"[:1000])
                    if f.rule_id.startswith("migration/destructive-")
                    else f
                    for f in verdict.findings
                )
                return replace(verdict, findings=accepted, message=f"{verdict.message}; destructive changes approved")
            if state.status == "REJECTED":
                return replace(verdict, status="FAIL", message=f"{APPROVAL_ACTION} was rejected ({state.approval_id})")
            if self._approvals is None:
                return replace(
                    verdict,
                    status="FAIL",
                    message=f"APPROVAL_REQUIRED: {APPROVAL_ACTION} needed, approvals port is not configured",
                )
            approval_id = state.approval_id
            if state.status != "PENDING":
                approval_id = await self._approvals.request(
                    conn,
                    workspace_id=task.workspace_id,
                    project_id=task.project_id,
                    run_id=task.run_id,
                    task_id=task.task_id,
                    action_type=APPROVAL_ACTION,
                    risk_level="high",
                    subject=subject,
                    requested_by_type="system",
                )
        return replace(
            verdict, status="FAIL", message=f"APPROVAL_REQUIRED: {APPROVAL_ACTION} pending (approval {approval_id})"
        )

    def _gate_context(self, task: _TaskContext, gate: _GateRun | None = None) -> GateContext:
        changed = changed_python_files(task.facts) if task.facts is not None else ()
        changed_all = task.facts.changed_paths if task.facts is not None else ()
        common: dict[str, Any] = {
            "profile": task.profile,
            "config": dict(gate.config) if gate is not None else {},
            "facts": task.facts,
            "changed_files": changed_all,
            "nonce": uuid.uuid4().hex,
        }
        if self._is_docker:
            return GateContext(CONTAINER_WORKDIR, CONTAINER_INPUT_DIR, changed, **common)
        return GateContext(str(task.worktree), str(task.staging or task.worktree), changed, **common)

    def _image_for(self, spec: GateSpec) -> str:
        """Образ gate: явное переопределение по имени gate → по ключу инструмента → образ по умолчанию."""
        return self._images.get(spec.name) or self._images.get(image_key_for(spec.tool, spec.image_key)) or self._image

    def _spec_for(
        self, task: _TaskContext, spec: GateSpec, argv: Sequence[str], limit_seconds: int, gate: _GateRun
    ) -> ContainerSpec:
        image = self._image_for(spec)
        labels = {
            "aicd.execution_id": f"verify-{gate.execution_id}",
            "aicd.kind": "verification",
            "aicd.gate": spec.name,
        }
        limits = ResourceLimits(
            cpus=self._limits.cpus,
            memory_mb=self._limits.memory_mb,
            pids=self._limits.pids,
            timeout_seconds=limit_seconds + 30,
            max_output_bytes=self._max_output,
        )
        if self._is_docker:
            mounts = [Mount(task.worktree, CONTAINER_WORKDIR, read_only=True)]
            if (spec.needs_scope_input or spec.needs_input_dir) and task.staging is not None:
                mounts.append(Mount(task.staging, CONTAINER_INPUT_DIR, read_only=True))
            env = {
                "CI": "true",
                "TZ": "UTC",
                "LANG": "C.UTF-8",
                "HOME": "/home/verify",
                "PYTHONDONTWRITEBYTECODE": "1",
                "RUFF_CACHE_DIR": "/tmp/ruff-cache",  # noqa: S108 — tmpfs
                "npm_config_cache": "/tmp/npm-cache",  # noqa: S108 — tmpfs контейнера
                "npm_config_update_notifier": "false",
            }
            spec_kwargs: dict[str, Any] = {"user": spec.container_user} if spec.container_user else {}
            return ContainerSpec(
                image=image,
                argv=tuple(argv),
                workdir=CONTAINER_WORKDIR,
                env=env,
                mounts=tuple(mounts),
                limits=limits,
                network="none",
                labels=labels,
                **spec_kwargs,
            )
        home = task.extra.setdefault("home", tempfile.mkdtemp(prefix="aicd-verify-home-", dir=self._staging_root))
        env = {
            "CI": "true",
            "TZ": "UTC",
            "LANG": "C.UTF-8",
            "HOME": home,
            "PYTHONDONTWRITEBYTECODE": "1",
            "npm_config_update_notifier": "false",
            **self._local_env,
        }
        return ContainerSpec(
            image=image,
            argv=tuple(argv),
            workdir=str(task.worktree),
            env=env,
            limits=limits,
            labels=labels,
            cwd=task.worktree,
        )

    async def _execute(
        self, task: _TaskContext, spec: GateSpec, argv: Sequence[str], limit_seconds: int, gate: _GateRun
    ) -> ProcessResult:
        container = self._spec_for(task, spec, argv, limit_seconds, gate)
        started_at = utcnow()
        try:
            proc = await self._runner.start(container)
        except (OSError, DockerError, ValueError) as exc:
            return ProcessResult(None, b"", b"", started_at, utcnow(), start_error=type(exc).__name__)
        image_id: str | None = None
        if isinstance(self._runner, DockerSandboxRunner):
            with contextlib.suppress(Exception):
                info = await self._runner.inspect(proc.ref)
                image_id = str(info.get("Image")) if info else None
        stdout, stderr = bytearray(), bytearray()
        truncated = False

        async def collect() -> int:
            nonlocal truncated
            async for chunk in proc.output():
                buf = stdout if chunk.stream == "stdout" else stderr
                room = self._max_output - len(stdout) - len(stderr)
                if room <= 0:
                    truncated = True
                    continue
                buf.extend(chunk.data[:room])
                truncated = truncated or len(chunk.data) > room
            return await proc.wait()

        timed_out = False
        try:
            code = await asyncio.wait_for(collect(), timeout=limit_seconds)
        except TimeoutError:
            timed_out = True
            code = await self._kill(proc)
        return ProcessResult(
            code,
            bytes(stdout),
            bytes(stderr),
            started_at,
            utcnow(),
            timed_out=timed_out,
            truncated=truncated,
            image_id=image_id,
        )

    @staticmethod
    async def _kill(proc: SandboxProcess) -> int:
        with contextlib.suppress(Exception):
            await proc.terminate(grace_seconds=5.0)
        try:
            return await asyncio.wait_for(proc.wait(), timeout=60)
        except Exception:  # noqa: BLE001 — процесс уже убит; код не важен, вердикт ERROR(TIMEOUT)
            return -9

    def _environment(self, spec: GateSpec, tool_version: str, result: ProcessResult) -> dict[str, Any]:
        env: dict[str, Any] = {
            "runner": "docker" if self._is_docker else "local",
            "tool_versions": {spec.tool: tool_version},
            "env_allowlist": {"CI": "true", "TZ": "UTC", "LANG": "C.UTF-8"},
            "verifier_host": platform.node()[:255],
        }
        if self._is_docker:
            env.update(
                {
                    "image": self._image_for(spec),
                    "image_id": result.image_id,
                    "network": "none",
                    "workdir": CONTAINER_WORKDIR,
                    "workspace_mount": "read-only",
                }
            )
        else:
            env.update({"image": None, "network": "host", "workdir": "worktree", "workspace_mount": "host"})
        if result.truncated:
            env["output_truncated"] = True
        return env

    async def _store_output(
        self, task: _TaskContext, gate: _GateRun, command: str, result: ProcessResult, environment: dict[str, Any]
    ) -> dict[str, Any]:
        assert gate.spec is not None  # noqa: S101 — вызывается только для известного gate
        body = (
            f"# aicd verification gate log\n# gate: {gate.name} ({gate.gate_type})\n# commit: {task.commit_sha}\n"
            f"# command: {command}\n# exit_code: {result.exit_code}\n# timed_out: {result.timed_out}\n"
            f"# started_at: {result.started_at.isoformat()}\n# finished_at: {result.finished_at.isoformat()}\n"
            f"--- stdout ---\n{result.stdout.decode('utf-8', 'replace')}\n"
            f"--- stderr ---\n{result.stderr.decode('utf-8', 'replace')}\n"
        )
        data = redact_text(body).encode("utf-8")
        artifact_id = uuid7()
        stored = await asyncio.to_thread(self._store.put, task.workspace_id, artifact_id, io.BytesIO(data))
        status = "VERIFIED"
        try:  # повторное чтение со сверкой хеша (QUALITY_GATES §4.2)
            reread = await asyncio.to_thread(
                lambda: b"".join(self._store.open(stored.storage_ref, expected_sha256=stored.sha256))
            )
            if reread != data:
                status = "REJECTED"
        except (ArtifactIntegrityError, ArtifactNotFoundError, OSError):
            status = "REJECTED"
        return {
            "artifact_id": artifact_id,
            "artifact_kind": gate.spec.artifact_kind,
            "storage_ref": stored.storage_ref,
            "sha256": stored.sha256,
            "size": stored.size_bytes,
            "evidence_kind": gate.spec.evidence_kind,
            "verification_status": status,
            "command": redact_text(command),
            "exit_code": result.exit_code,
            "environment": environment,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
        }

    # ---------------------------------------------------------------- вердикт
    def _verdict(self, gates: list[_GateRun]) -> VerificationOutcome:
        ids = [g.execution_id for g in gates if g.execution_id is not None]
        required_ok = all(g.status in OK_FOR_COMPLETION for g in gates if g.required)
        any_pass = any(g.status == "PASS" for g in gates)
        parts = []
        for g in gates:
            flag = "required" if g.required else "optional"
            note = f" ({g.detail[:120]})" if g.detail and g.status not in OK_FOR_COMPLETION else ""
            parts.append(f"{g.name}[{g.gate_type},{flag}]={g.status}{' (reused)' if g.reused else ''}{note}")
        return self._outcome(required_ok and any_pass, ids, "; ".join(parts))

    def _outcome(self, passed: bool, ids: Sequence[uuid.UUID], summary: str) -> VerificationOutcome:
        return VerificationOutcome(passed=passed, gate_execution_ids=tuple(ids), summary=summary)
