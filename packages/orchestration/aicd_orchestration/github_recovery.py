"""Recovery-сверка прерванных GitHub-действий ledger (OQ-47 (7), ADR-0012, AC-09; DATA_FLOW §7).

До Phase 9 строка `external_actions` GitHub-цепочки (`git_push` → `pr_create` → `pr_comment` → `pr_update`,
и `pr_merge`), оставшаяся в `EXECUTING` после гибели воркера, сверялась только при повторной доставке
сообщения Dramatiq. Если сообщение потеряно (Redis без persistence, событие outbox уже помечено
опубликованным), действие висело бы бесконечно. Этот модуль — проход reconciler'а воркера-оркестратора
(там, где живёт GitHub App; на exec-воркер ключ App не попадает):

1. `EXECUTING`-действия GitHub без живой аренды с их fencing token (аренда истекла/освобождена);
2. **probe** внешней системы по данным строки ledger (`request`/`target`) — тот же критерий, что у
   `execute_once`: ветка на нужном SHA; PR из head-ветки существует; ревью с маркером `aicd:review:<id>`;
   commit status `aicd/verification` в ожидаемом состоянии; PR смёрджен. Применено ⇒ `CONFIRMED`
   (`result.reconciled = true`) под тем же fencing token, без повторного вызова GitHub;
3. **resume** — повторный вход в идемпотентный workflow (`open_pull_request` / `merge`) с новой арендой:
   оставшиеся шаги цепочки исполняются через `execute_once` (probe перед любым `do`), т. е. слепого повтора
   нет и после неподтверждённого probe. Параллельная доставка Dramatiq не приводит к дублю: аренда
   `external_action` одна (вторая копия получает отказ и повторяется позже).

Не применено и resume невозможен (например, установка App отключена) ⇒ строка остаётся `EXECUTING` и
попадает в отчёт `unreconciled` (алерт по логу), как и для прочих действий без probe.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import text

from aicd_integrations_github.api import GitHubError, store
from aicd_orchestration import external_actions
from aicd_orchestration.github_flow import STATUS_CONTEXT, GitHubWorkflow, _gate_summary, load_binding
from aicd_platform import audit
from aicd_platform.db import DbContext, transaction
from aicd_platform.errors import ProblemError

log = logging.getLogger("aicd.github.recovery")

GITHUB_ACTIONS: Final = ("git_push", "pr_create", "pr_comment", "pr_update", "pr_merge")
PR_CHAIN: Final = frozenset({"git_push", "pr_create", "pr_comment", "pr_update"})


@dataclass
class GitHubRecoveryReport:
    reconciled: list[uuid.UUID] = field(default_factory=list)  # probe: уже применено ⇒ CONFIRMED
    resumed: list[uuid.UUID] = field(default_factory=list)  # workflow доведён до конца
    unreconciled: list[uuid.UUID] = field(default_factory=list)  # остаётся EXECUTING (алерт)


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value is not None else None
    except ValueError:
        return None


async def interrupted(workflow: GitHubWorkflow, ws: uuid.UUID) -> list[uuid.UUID]:
    """EXECUTING GitHub-действия workspace без живой аренды с их fencing token."""
    async with transaction(workflow.engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
        rows = await conn.execute(
            text(
                "SELECT x.id FROM external_actions x WHERE x.status = 'EXECUTING' AND x.action_type = ANY(:types)"
                " AND NOT EXISTS (SELECT 1 FROM worker_leases l WHERE l.fencing_token = x.fencing_token"
                " AND l.released_at IS NULL AND l.lease_until >= now()) ORDER BY x.executing_at, x.id"
            ),
            {"types": list(GITHUB_ACTIONS)},
        )
        return list(rows.scalars().all())


async def probe(workflow: GitHubWorkflow, action: external_actions.ExternalAction) -> external_actions.Outcome | None:
    """Состояние GitHub по строке ledger: Outcome — действие уже применено, None — нет/неизвестно."""
    ctx = DbContext(workspace_id=action.workspace_id, actor_type="system")
    request = action.request
    async with transaction(workflow.engine, ctx) as conn:
        pr: dict[str, Any] | None = None
        if action.action_type == "pr_merge":
            pr_id = _uuid(request.get("pull_request_id"))
            pr = await store.get_pull_request(conn, pr_id) if pr_id else None
            if pr is None:
                return None
            repository_id = pr["repository_id"]
        else:
            repository_id = _uuid(request.get("repository_id"))
            if repository_id is None:
                return None
        binding = await load_binding(conn, repository_id)
        number: int | None = None
        if action.action_type == "pr_comment" and action.run_id is not None:
            number = (
                await conn.execute(
                    text(
                        "SELECT pr_number FROM pull_requests WHERE run_id = :r AND repository_id = :repo"
                        " ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"r": action.run_id, "repo": repository_id},
                )
            ).scalar_one_or_none()
        summary = (
            await _gate_summary(conn, action.run_id)
            if action.action_type == "pr_update" and action.run_id is not None
            else None
        )
    repo = binding.ref
    token = await workflow.client.installation_token(
        binding.installation_id, "read", repository_ids=[binding.github_repo_id]
    )
    head_sha = str(request.get("head_sha") or "")
    if action.action_type == "git_push":
        branch_name = str(request.get("branch") or "")
        branch = await workflow.client.branch(token, repo, branch_name)
        if branch is not None and branch.sha == head_sha:
            return external_actions.Outcome(f"{repo.full_name}@{head_sha}", {"branch": branch_name})
        return None
    if action.action_type == "pr_create":
        pulls = await workflow.client.find_pulls(token, repo, str(request.get("head") or ""))
        if pulls:
            return external_actions.Outcome(pulls[0].url, {"number": pulls[0].number, "head_sha": pulls[0].head_sha})
        return None
    if action.action_type == "pr_comment":
        if number is None:
            return None  # PR ещё не записан ⇒ ревью точно не создавалось этой цепочкой до pr_create
        marker = f"<!-- aicd:review:{action.id} -->"
        for item in await workflow.client.reviews(token, repo, int(number)):
            if marker in str(item.get("body") or ""):
                return external_actions.Outcome(str(item.get("id")), {"pr_number": int(number)})
        return None
    if action.action_type == "pr_update":
        if summary is None or not action.target.endswith(f"#status:{STATUS_CONTEXT}"):
            return None
        state = "success" if summary["failed"] == 0 else "failure"
        statuses = await workflow.client.commit_statuses(token, repo, head_sha)
        if statuses.get(STATUS_CONTEXT) == state:
            return external_actions.Outcome(f"{repo.full_name}@{head_sha}", {"state": state})
        return None
    if action.action_type == "pr_merge" and pr is not None:
        pull = await workflow.client.pull(token, repo, int(pr["pr_number"]))
        if pull.merged and pull.merge_commit_sha:
            return external_actions.Outcome(f"{repo.full_name}#{pull.number}", {"merged_sha": pull.merge_commit_sha})
    return None


async def _confirm(workflow: GitHubWorkflow, action: external_actions.ExternalAction, observed: Any) -> bool:
    """CONFIRMED под fencing token строки — только если она всё ещё EXECUTING без живой аренды."""
    ctx = DbContext(workspace_id=action.workspace_id, actor_type="system")
    async with transaction(workflow.engine, ctx) as conn:
        current = await external_actions.get(conn, action.id, lock=True)
        if current is None or current.status != "EXECUTING" or current.fencing_token != action.fencing_token:
            return False  # действие перехватил живой воркер — сверка не нужна
        live = (
            await conn.execute(
                text(
                    "SELECT 1 FROM worker_leases WHERE fencing_token = :t AND released_at IS NULL"
                    " AND lease_until >= now()"
                ),
                {"t": current.fencing_token},
            )
        ).first()
        if live is not None:
            return False
        await external_actions.confirm(
            conn,
            action.id,
            fencing_token=int(current.fencing_token or 0),
            external_ref=observed.external_ref,
            result={**observed.result, "reconciled": True, "reconciled_by": "recovery"},
        )
        await audit.record(
            conn,
            action="external_action.reconciled",
            resource_type="external_action",
            resource_id=action.id,
            actor_type="system",
            actor_id=None,
            workspace_id=action.workspace_id,
            metadata={"action_type": action.action_type, "external_ref": observed.external_ref},
        )
    return True


async def _pr_job(workflow: GitHubWorkflow, action: external_actions.ExternalAction) -> dict[str, Any] | None:
    """Параметры исходного запроса PR — из строки `pr_create` той же цепочки (run, task)."""
    if action.run_id is None or action.task_id is None:
        return None
    async with transaction(workflow.engine, DbContext(workspace_id=action.workspace_id, actor_type="system")) as conn:
        request = (
            await conn.execute(
                text(
                    "SELECT request FROM external_actions WHERE run_id = :r AND task_id = :t"
                    " AND action_type = 'pr_create' ORDER BY id LIMIT 1"
                ),
                {"r": action.run_id, "t": action.task_id},
            )
        ).scalar_one_or_none()
    if not isinstance(request, dict):
        return None
    return {
        "run_id": str(action.run_id),
        "task_id": str(action.task_id),
        "base_branch": request.get("base"),
        "title": request.get("title"),
    }


async def _apply_merged(workflow: GitHubWorkflow, action: external_actions.ExternalAction) -> None:
    """После сверки merge строка PR получает состояние из GitHub API (workflow.merge при CONFIRMED её не трогает)."""
    ctx = DbContext(workspace_id=action.workspace_id, actor_type="system")
    async with transaction(workflow.engine, ctx) as conn:
        pr_id = _uuid(action.request.get("pull_request_id"))
        pr = await store.get_pull_request(conn, pr_id) if pr_id else None
        if pr is None or pr["state"] == "MERGED":
            return
        binding = await load_binding(conn, pr["repository_id"])
    token = await workflow.client.installation_token(
        binding.installation_id, "read", repository_ids=[binding.github_repo_id]
    )
    pull = await workflow.client.pull(token, binding.ref, int(pr["pr_number"]))
    async with transaction(workflow.engine, ctx) as conn:
        await store.apply_remote_state(conn, pr["id"], pull, merge_action_id=action.id)


async def _resume(workflow: GitHubWorkflow, action: external_actions.ExternalAction) -> bool:
    ws = action.workspace_id
    if action.action_type in PR_CHAIN:
        job = await _pr_job(workflow, action)
        return job is not None and await workflow.open_pull_request(ws, job) == "opened"
    if action.action_type == "pr_merge" and action.approval_id is not None:
        outcome = await workflow.merge(ws, action.approval_id)
        if outcome == "merged":
            await _apply_merged(workflow, action)
        return outcome == "merged"
    return False


async def reconcile_workspace(workflow: GitHubWorkflow, ws: uuid.UUID, report: GitHubRecoveryReport) -> None:
    for action_id in await interrupted(workflow, ws):
        async with transaction(workflow.engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
            action = await external_actions.get(conn, action_id)
        if action is None or action.status != "EXECUTING":
            continue
        try:
            observed = await probe(workflow, action)
            if observed is not None and await _confirm(workflow, action, observed):
                report.reconciled.append(action_id)
            if await _resume(workflow, action):
                report.resumed.append(action_id)
                continue
        except (GitHubError, ProblemError, RuntimeError, external_actions.LedgerError) as exc:
            log.warning(
                "github action resume failed",
                extra={"action_id": str(action_id), "action_type": action.action_type, "error": str(exc)[:300]},
            )
        async with transaction(workflow.engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
            after = await external_actions.get(conn, action_id)
        if after is not None and after.status == "EXECUTING":
            report.unreconciled.append(action_id)
            log.warning(
                "external action needs reconciliation",
                extra={"action_id": str(action_id), "action_type": action.action_type},
            )


async def reconcile(workflow: GitHubWorkflow, *, workspace_limit: int = 200) -> GitHubRecoveryReport:
    """Проход по workspace с EXECUTING-действиями (`sys_recovery_workspaces`)."""
    report = GitHubRecoveryReport()
    async with transaction(workflow.engine, DbContext(actor_type="system")) as conn:
        workspaces = (
            (await conn.execute(text("SELECT workspace_id FROM sys_recovery_workspaces(:n)"), {"n": workspace_limit}))
            .scalars()
            .all()
        )
    for ws in workspaces:
        try:
            await reconcile_workspace(workflow, ws, report)
        except Exception:  # одна «битая» запись не должна останавливать восстановление остальных
            log.exception("github recovery failed for workspace", extra={"workspace_id": str(ws)})
    return report
