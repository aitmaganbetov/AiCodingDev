"""Phase 4: ввод-вывод агента в исполнении задачи (AGENT_ROLES §3.3–§3.4, ACP §1, §5, §9.2) на PostgreSQL.

`CliTaskExecutor` с Fake CLI (настоящий процесс) и настоящими gateway/worktree/брокером ACP под ролью
`aicd_worker`: промпт роли сохраняется artifact-ом (блок политики первым), итог `.aicd/result.json` читается
из каталога вывода исполнения, ACP-кандидаты проходят брокер с отправителем из задачи/попытки, невалидные
итоги не роняют исполнение, `.aicd/` и `output/` не попадают в коммит задачи.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from aicd_cli_adapters.fake.adapter import ScenarioSpec
from aicd_orchestration.cli_executor import CliTaskExecutor
from aicd_orchestration.engine import TaskExecution
from aicd_platform.artifacts import FileSystemArtifactStore
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from aicd_security.paths import resolve_ref
from tests.support import db as testdb
from tests.support import orchestration as orch
from tests.support.phase3 import Slice, build_executor, install_both, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db


@dataclass
class Scene:
    s: Slice
    database: testdb.TestDatabase
    ws: uuid.UUID
    project: uuid.UUID
    run: uuid.UUID
    base: str
    installations: dict[str, uuid.UUID]

    def admin(self) -> Any:
        return orch.connect_admin(self.database)

    def task(self, role: str, *, provider: str, key: str | None = None) -> tuple[uuid.UUID, uuid.UUID]:
        """Задача роли в RUNNING run + аренда + попытка RUNNING (fencing совпадает)."""
        task, attempt = uuid7(), uuid7()
        with self.admin() as conn:
            conn.execute(
                "INSERT INTO tasks (id, workspace_id, run_id, plan_version, task_key, agent_role, title, instructions,"
                " complexity, risk_level, acceptance_criteria)"
                " VALUES (%s, %s, %s, 1, %s, %s, 'Задача', 'Добавить документ', 2, 'low', '[\"Файл создан\"]')",
                (task, self.ws, self.run, key or f"{role}-{task.hex[-6:]}", role),
            )
            orch.lease_task(conn, task, token=7)
            conn.execute(
                "INSERT INTO task_attempts (id, workspace_id, task_id, attempt_no, provider, fencing_token)"
                " VALUES (%s, %s, %s, 1, %s, 7)",
                (attempt, self.ws, task, provider),
            )
        return task, attempt

    def executor(self, scenario: ScenarioSpec) -> CliTaskExecutor:
        return build_executor(self.s.worker, self.s.tmp, self.s.settings.repository_roots, scenario)

    async def execute(
        self, executor: CliTaskExecutor, task_id: uuid.UUID, attempt_id: uuid.UUID, provider: str
    ) -> TaskExecution:
        async with transaction(self.s.worker, DbContext(workspace_id=self.ws, actor_type="system")) as conn:
            task = (await conn.execute(text("SELECT * FROM tasks WHERE id = :id"), {"id": task_id})).mappings().one()
            run = (await conn.execute(text("SELECT * FROM runs WHERE id = :id"), {"id": self.run})).mappings().one()
        outcome = await executor.execute(
            task=dict(task),
            run=dict(run),
            attempt_id=attempt_id,
            provider=provider,
            installation_id=self.installations[provider],
            register_cancel=lambda hook: None,
        )
        assert isinstance(outcome, TaskExecution) and outcome.agent_io is not None
        return outcome

    def rows(self, sql: str, *params: Any) -> list[tuple[Any, ...]]:
        with self.admin() as conn:
            return list(conn.execute(sql, params).fetchall())


@pytest.fixture
async def scene(world: Slice, database: testdb.TestDatabase) -> Scene:
    await install_both(world.worker)
    ws, project = uuid.UUID(world.ws), uuid.UUID(world.project)
    with orch.connect_admin(database) as conn:
        repo = conn.execute("SELECT id, local_root_ref FROM repositories WHERE project_id = %s", (project,)).fetchone()
        owner = conn.execute("SELECT created_by FROM workspaces WHERE id = %s", (ws,)).fetchone()
        rows = conn.execute(
            "SELECT p.code, i.id FROM cli_installations i JOIN cli_providers p ON p.id = i.provider_id WHERE i.enabled"
        ).fetchall()
    assert repo is not None and owner is not None
    installations: dict[str, uuid.UUID] = {str(code): iid for code, iid in rows}
    base = _git(_repo_path(world, repo[1]), "rev-parse", "HEAD").strip()
    run = uuid7()
    with orch.connect_admin(database) as conn:
        conn.execute(
            "INSERT INTO runs (id, workspace_id, project_id, repository_id, requested_by, mode, objective,"
            " quality_profile, quality_profile_id, documentation_mode, base_commit_sha)"
            " VALUES (%s, %s, %s, %s, %s, 'DEVELOP', 'Добавить документ', 'strict', %s, 'SMART', %s)",
            (run, ws, project, repo[0], owner[0], orch.STRICT_PROFILE_ID, base),
        )
        orch.drive_run(conn, run, "DISCOVERY", "PLANNED", "RUNNING")
    return Scene(world, database, ws, project, run, base, installations)


def _repo_path(world: Slice, ref: str) -> Path:
    return resolve_ref(ref, world.settings.repository_roots)


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout


def _messages(scene: Scene, task_id: uuid.UUID) -> dict[str, tuple[Any, ...]]:
    rows = scene.rows(
        "SELECT message_type, sender_role, sender_provider, task_id, task_attempt_id, source_execution_id,"
        " recipient_role, payload, id, correlation_id, reply_to_id FROM agent_messages WHERE task_id = %s",
        task_id,
    )
    return {r[0]: r for r in rows}


async def test_develop_review_fix_flow(scene: Scene) -> None:
    """ACP §9.2: developer → EVIDENCE_SUBMITTED + REVIEW_REQUEST + TASK_COMPLETED; reviewer → FINDING + FIX_REQUEST."""
    executor = scene.executor({"developer": "agent_develop", "reviewer": "agent_review_changes"})
    dev_task, dev_attempt = scene.task("developer", provider="codex", key="dev")
    dev = await scene.execute(executor, dev_task, dev_attempt, "codex")
    io = dev.agent_io
    assert dev.status == "completed" and io is not None
    assert io.result_status == "parsed", io.result_error
    assert io.instruction_template_version == "developer@1.0.0"
    assert io.rejected == () and len(io.accepted_message_ids) == 3
    assert io.claims and all(c.verified is False for c in io.claims), "claims ≠ evidence"

    # Промпт: artifact context_pack (sensitive), блок политики первым, sha256 совпадает.
    (kind, ref, sha, sensitive, project), *_ = scene.rows(
        "SELECT kind, storage_ref, sha256, is_sensitive, project_id FROM artifacts WHERE id = %s", io.prompt_artifact_id
    )
    assert kind == "context_pack" and sensitive and project == scene.project and sha == io.prompt_sha256
    prompt = b"".join(FileSystemArtifactStore(scene.s.tmp / "artifacts").open(ref, expected_sha256=sha)).decode()
    assert prompt.startswith("<<<BLOCK 1 SYSTEM_POLICY nonce=")
    assert prompt.index("SYSTEM_POLICY") < prompt.index("ROLE_INSTRUCTIONS") < prompt.index("UNTRUSTED_DATA")
    assert scene.rows(
        "SELECT payload ->> 'prompt_sha256' FROM agent_activity_events WHERE task_id = %s"
        " AND event_type = 'agent.prompt_assembled'",
        dev_task,
    ) == [(io.prompt_sha256,)]

    # Коммит задачи: изменение агента есть, каталоги ввода-вывода агента — нет (ни в коммите, ни в дереве).
    assert dev.head_commit_sha and dev.head_commit_sha != scene.base
    files = _git(dev.worktree_path, "show", "--name-only", "--format=", dev.head_commit_sha).split()
    assert files == ["FAKE_CHANGE.md"]
    assert not (dev.worktree_path / ".aicd").exists() and not (dev.worktree_path / "output").exists()

    # ACP: отправитель — из задачи/попытки, а не из итога; ссылки output/ → artifact; commit_sha — снимок платформы.
    msgs = _messages(scene, dev_task)
    assert set(msgs) == {"EVIDENCE_SUBMITTED", "REVIEW_REQUEST", "TASK_COMPLETED"}
    for row in msgs.values():
        assert row[1:6] == ("developer", "codex", dev_task, dev_attempt, dev.execution_id)
    review_request = msgs["REVIEW_REQUEST"]
    assert review_request[6] == "reviewer"
    assert review_request[7]["commit_sha"] == dev.head_commit_sha
    diff_artifact = uuid.UUID(review_request[7]["diff_ref"].removeprefix("artifact:"))
    assert diff_artifact == io.output_artifacts["output/changes.diff"]
    evidence = msgs["EVIDENCE_SUBMITTED"][7]["items"][0]
    assert scene.rows("SELECT sha256, project_id FROM artifacts WHERE id = %s", diff_artifact) == [
        (evidence["sha256"], scene.project)
    ]

    # Reviewer (write_scope=none): inbox с REVIEW_REQUEST, итог — в каталоге вывода, не в worktree.
    rev_task, rev_attempt = scene.task("reviewer", provider="claude", key="review")
    rev = await scene.execute(executor, rev_task, rev_attempt, "claude")
    rio = rev.agent_io
    assert rio is not None and rio.result_status == "parsed", rio and rio.result_error
    assert rio.inbox_message_ids == (review_request[8],)
    assert rio.review_verdict == "changes_requested"
    assert rio.rejected == () and len(rio.accepted_message_ids) == 3
    assert rev.head_commit_sha == scene.base, "роль только для чтения не меняет код"
    assert not (rev.worktree_path / ".aicd").exists()
    assert set(rio.output_artifacts) == {"output/review.md"}
    rmsgs = _messages(scene, rev_task)
    assert set(rmsgs) == {"FINDING", "FIX_REQUEST", "TASK_COMPLETED"}
    fix = rmsgs["FIX_REQUEST"]
    assert fix[1:5] == ("reviewer", "claude", rev_task, rev_attempt)
    assert fix[6] == "developer" and fix[10] == review_request[8] and fix[9] == review_request[9]
    assert fix[7]["evidence_refs"] == [f"artifact:{rio.output_artifacts['output/review.md']}"]
    assert scene.rows("SELECT review_loop_count FROM tasks WHERE id = %s", dev_task) == [(1,)]
    prompt_rows = scene.rows(
        "SELECT payload -> 'inbox_message_ids' FROM agent_activity_events WHERE task_id = %s"
        " AND event_type = 'agent.prompt_assembled'",
        rev_task,
    )
    assert prompt_rows == [([str(review_request[8])],)]


async def test_reviewer_approves_by_role_mapping(scene: Scene) -> None:
    executor = scene.executor("agent")  # сценарий по роли: reviewer → agent_review_approve
    task, attempt = scene.task("reviewer", provider="claude")
    out = await scene.execute(executor, task, attempt, "claude")
    assert out.agent_io is not None and out.agent_io.review_verdict == "approved"
    assert set(_messages(scene, task)) == {"TASK_COMPLETED"}


async def test_missing_result_is_reported_not_fatal(scene: Scene) -> None:
    executor = scene.executor("success_diff")  # сценарий Phase 3: итога AgentResult нет
    task, attempt = scene.task("developer", provider="codex")
    out = await scene.execute(executor, task, attempt, "codex")
    assert out.status == "completed" and out.agent_io is not None
    assert out.agent_io.result_status == "missing" and out.agent_io.accepted_message_ids == ()
    assert _git(out.worktree_path, "show", "--name-only", "--format=", str(out.head_commit_sha)).split() == [
        "FAKE_CHANGE.md"
    ]


@pytest.mark.parametrize("scenario", ["agent_bad_json", "agent_spoofed_from"])
async def test_invalid_result_rejected_without_crash(scene: Scene, scenario: str) -> None:
    executor = scene.executor(scenario)
    task, attempt = scene.task("developer", provider="codex")
    out = await scene.execute(executor, task, attempt, "codex")
    assert out.status == "completed" and out.agent_io is not None
    assert out.agent_io.result_status == "invalid"
    assert out.agent_io.result_error and out.agent_io.result_error.startswith("OUTPUT_PARSE_ERROR")
    assert _messages(scene, task) == {}
    assert scene.rows(
        "SELECT payload ->> 'result_status' FROM agent_activity_events WHERE task_id = %s"
        " AND event_type = 'agent.result_processed'",
        task,
    ) == [("invalid",)]


async def test_acl_violations_rejected_valid_message_accepted(scene: Scene) -> None:
    executor = scene.executor("agent_acl_violation")
    task, attempt = scene.task("developer", provider="codex")
    out = await scene.execute(executor, task, attempt, "codex")
    io = out.agent_io
    assert out.status == "completed" and io is not None and io.result_status == "parsed"
    assert [(r.stage, r.type, r.code) for r in io.rejected] == [
        ("result", "FIX_REQUEST", "ACL_DENIED"),  # отправитель: developer не может FIX_REQUEST
        ("broker", "TASK_COMPLETED", "ACL_DENIED"),  # получатель: TASK_COMPLETED — только orchestrator
    ]
    assert len(io.accepted_message_ids) == 1 and not io.attempt_should_fail
    assert set(_messages(scene, task)) == {"TASK_COMPLETED"}
    denials = scene.rows(
        "SELECT payload ->> 'rejection_code' FROM agent_activity_events WHERE event_type = 'acp.rejected'"
        " AND payload ->> 'task_attempt_id' = %s",
        str(attempt),
    )
    assert sorted(d[0] for d in denials) == ["ACL_DENIED", "ACL_DENIED"]
