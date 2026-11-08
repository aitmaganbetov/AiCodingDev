"""PR workflow GitHub через ledger `external_actions` (P8-04, P8-05; ADR-0006, ADR-0012, AC-09, AC-13).

Запрос PR (`POST /runs/{run_id}/pull-requests`) фиксирует в API только событие outbox `github.pr.requested`
(запись ledger — привилегия воркера). Воркер (`GitHubWorkflow.open_pull_request`):

1. источник — проверенная работа завершённого run: последний worktree задачи SUCCEEDED с коммитом,
   на котором есть PASS gate (`load_pr_source`); ветка — ветка worktree `aicd/<run>/<task>-<wt>`;
2. изменения CI-конфигурации (`.github/workflows/**` и аналоги) не пушатся без APPROVED `MANUAL_REVIEW`
   этого предмета (SC-GH-05): approval запрашивается, PR не создаётся;
3. действия ledger с детерминированными ключами: `git_push` → `pr_create` → `pr_comment` (ревью с итогами
   gates) → `pr_update` (commit status `aicd/verification`); каждое — `execute_once` с fencing token аренды
   `external_action` (pr_create). EXECUTING после краха сначала сверяется с GitHub: ветка уже на нужном
   SHA, PR из этой head-ветки уже существует, ревью с маркером уже есть, статус уже выставлен — тогда
   CONFIRMED без повтора (crash-тест: PR не создаётся дважды). `do()` создания PR дополнительно ищет PR по
   head-ветке перед созданием.

Merge (P8-05): `request_merge` (API) создаёт approval `MERGE` (admin, four-eyes) на предмет
{PR, head SHA, base, метод}. После `APPROVED` воркер (`merge`) планирует `pr_merge` с `approval_id` и
`request = subject` (⇒ `request_hash = subject_hash`; триггер БД не пустит EXECUTING без APPROVED approval),
перечитывает PR и защиту базовой ветки через GitHub API (required checks, `mergeable_state`), и только если
всё выполнено — merge с `sha = head SHA approval` (сдвиг ветки ⇒ 409, без обхода protection, без admin
merge). Вебхуки — подсказки: `process_webhook` перечитывает состояние PR/установки через API (SC-GH-03).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_integrations_github.api import (
    GitHubClient,
    GitHubError,
    InstallationToken,
    PullInfo,
    RepoRef,
    store,
)
from aicd_orchestration import external_actions
from aicd_orchestration.approvals import Approvals, subject_hash
from aicd_platform import audit, leases, outbox
from aicd_platform.db import DbContext, transaction
from aicd_platform.errors import FieldError, ProblemError
from aicd_platform.redaction import redact_text
from aicd_security.git import GitRepositoryError, push_ref, run_git
from aicd_security.paths import UnsafePathError, resolve_ref

log = logging.getLogger("aicd.github")

PR_REQUESTED_EVENT: Final = "github.pr.requested"
WEBHOOK_EVENT: Final = "github.webhook.received"
STATUS_CONTEXT: Final = "aicd/verification"
LEASE_TTL_SECONDS: Final = 600
MERGE_METHODS: Final = frozenset({"merge", "squash", "rebase"})
# mergeable_state GitHub, при которых merge допустим: требования защиты ветки выполнены (blocked/behind/
# dirty/draft/unknown — нет). `unstable` — упали только необязательные проверки.
MERGEABLE_STATES: Final = frozenset({"clean", "has_hooks", "unstable"})
CHECK_OK: Final = frozenset({"success", "neutral", "skipped"})
CI_CONFIG_PREFIXES: Final = (".github/workflows/", ".github/actions/", ".circleci/", ".buildkite/", ".gitlab/ci/")
CI_CONFIG_FILES: Final = frozenset(
    {".gitlab-ci.yml", "Jenkinsfile", "azure-pipelines.yml", "bitbucket-pipelines.yml", ".travis.yml", ".drone.yml"}
)

Outcome = Literal["opened", "blocked", "rejected", "merged", "denied", "ignored", "processed", "failed", "duplicate"]


class MergeBlockedError(RuntimeError):
    """Merge не выполняется: защита ветки/состояние PR/сдвиг head (без обхода)."""


@dataclass(frozen=True)
class RepoBinding:
    repository_id: uuid.UUID
    project_id: uuid.UUID
    full_name: str
    github_repo_id: int
    installation_id: int
    installation_status: str
    local_root_ref: str
    default_branch: str

    @property
    def ref(self) -> RepoRef:
        return RepoRef.parse(self.full_name)


@dataclass(frozen=True)
class PrSource:
    workspace_id: uuid.UUID
    run_id: uuid.UUID
    task_id: uuid.UUID
    objective: str
    branch: str
    head_sha: str
    base_sha: str
    repo: RepoBinding


async def load_binding(conn: AsyncConnection, repository_id: uuid.UUID) -> RepoBinding:
    row = (
        (
            await conn.execute(
                text(
                    "SELECT r.id, r.project_id, r.provider, r.full_name, r.github_repo_id, r.local_root_ref,"
                    " r.default_branch, i.installation_id, i.status AS installation_status"
                    " FROM repositories r LEFT JOIN github_installations i ON i.id = r.github_installation_id"
                    " WHERE r.id = :id"
                ),
                {"id": repository_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ProblemError("not_found")
    if row["provider"] != "github" or row["github_repo_id"] is None or row["installation_id"] is None:
        raise ProblemError("invalid_state_transition", detail="Репозиторий run не привязан к GitHub App")
    if row["installation_status"] != "ACTIVE":
        raise ProblemError("invalid_state_transition", detail="Установка GitHub App не активна")
    return RepoBinding(
        repository_id=row["id"],
        project_id=row["project_id"],
        full_name=str(row["full_name"]),
        github_repo_id=int(row["github_repo_id"]),
        installation_id=int(row["installation_id"]),
        installation_status=str(row["installation_status"]),
        local_root_ref=str(row["local_root_ref"]),
        default_branch=str(row["default_branch"]),
    )


async def load_pr_source(conn: AsyncConnection, run_id: uuid.UUID, task_id: uuid.UUID | None) -> PrSource:
    """Проверенная работа завершённого run для PR (404/409/422 — ProblemError)."""
    run = (
        (
            await conn.execute(
                text("SELECT id, workspace_id, status, repository_id, objective FROM runs WHERE id = :r"),
                {"r": run_id},
            )
        )
        .mappings()
        .first()
    )
    if run is None:
        raise ProblemError("not_found")
    if run["status"] != "COMPLETED":
        raise ProblemError(
            "invalid_state_transition",
            detail="PR создаётся только из завершённого (проверенного) run",
            extensions={"current_status": run["status"]},
        )
    repo = await load_binding(conn, run["repository_id"])
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT w.task_id, w.branch_name, w.base_commit_sha, w.head_commit_sha FROM worktrees w"
                    " JOIN tasks t ON t.id = w.task_id"
                    " WHERE w.run_id = :r AND t.status = 'SUCCEEDED' AND t.superseded_at IS NULL"
                    " AND w.head_commit_sha IS NOT NULL AND w.head_commit_sha <> w.base_commit_sha"
                    " AND (CAST(:t AS uuid) IS NULL OR w.task_id = :t)"
                    " AND EXISTS (SELECT 1 FROM gate_executions g WHERE g.run_id = w.run_id AND g.status = 'PASS'"
                    "             AND g.superseded_at IS NULL AND g.commit_sha = w.head_commit_sha)"
                    " ORDER BY w.created_at DESC, w.id DESC"
                ),
                {"r": run_id, "t": task_id},
            )
        )
        .mappings()
        .all()
    )
    latest: dict[uuid.UUID, Mapping[str, Any]] = {}
    for row in rows:
        latest.setdefault(row["task_id"], dict(row))
    if not latest:
        raise ProblemError(
            "validation_error",
            errors=[FieldError("/task_id", "no_changes", "нет проверенных изменений задачи для PR")],
        )
    if len(latest) > 1:
        raise ProblemError(
            "validation_error",
            errors=[FieldError("/task_id", "required", "в run несколько задач с изменениями — укажите task_id")],
        )
    chosen = next(iter(latest.values()))
    return PrSource(
        workspace_id=run["workspace_id"],
        run_id=run_id,
        task_id=chosen["task_id"],
        objective=str(run["objective"]),
        branch=str(chosen["branch_name"]),
        head_sha=str(chosen["head_commit_sha"]),
        base_sha=str(chosen["base_commit_sha"]),
        repo=repo,
    )


def repository_path(binding: RepoBinding, roots: Mapping[str, Path]) -> Path:
    try:
        return resolve_ref(binding.local_root_ref, dict(roots))
    except UnsafePathError as exc:
        raise ProblemError("invalid_state_transition", detail="Локальная копия репозитория недоступна") from exc


def changed_paths(repo: Path, base_sha: str, head_sha: str) -> list[str]:
    out = run_git(repo, "diff", "--name-only", "--no-renames", "--no-ext-diff", "--no-textconv", base_sha, head_sha)
    return [p for p in out.splitlines() if p]


def ci_config_paths(paths: list[str]) -> list[str]:
    """Изменения CI-конфигурации (push в тот же репозиторий запускает workflow пользователя, SC-GH-05)."""
    return sorted(p for p in paths if p.startswith(CI_CONFIG_PREFIXES) or p in CI_CONFIG_FILES)


def ci_push_subject(source: PrSource, paths: list[str]) -> dict[str, Any]:
    return {
        "scope": "github",
        "kind": "ci_config_push",
        "run_id": str(source.run_id),
        "task_id": str(source.task_id),
        "repository": source.repo.full_name,
        "head_sha": source.head_sha,
        "paths": paths[:200],
        "summary": "Push изменений CI-конфигурации в GitHub",
    }


async def approved(conn: AsyncConnection, action_type: str, subject: dict[str, Any]) -> uuid.UUID | None:
    """APPROVED, не истёкший approval этого предмета (subject_hash) в текущем workspace."""
    found = (
        await conn.execute(
            text(
                "SELECT id FROM approvals WHERE action_type = :at AND subject_hash = :h AND status = 'APPROVED'"
                " AND (expires_at IS NULL OR expires_at > now()) ORDER BY decided_at DESC LIMIT 1"
            ),
            {"at": action_type, "h": subject_hash(subject)},
        )
    ).scalar_one_or_none()
    return uuid.UUID(str(found)) if found else None


async def require_ci_push_approval(
    conn: AsyncConnection,
    source: PrSource,
    paths: list[str],
    *,
    requested_by: uuid.UUID | None,
    approvals: Approvals | None = None,
) -> uuid.UUID | None:
    """None — пуш разрешён; иначе id approval MANUAL_REVIEW (PENDING), которого ждёт пуш."""
    ci = ci_config_paths(paths)
    if not ci:
        return None
    subject = ci_push_subject(source, ci)
    if await approved(conn, "MANUAL_REVIEW", subject) is not None:
        return None
    return await (approvals or Approvals()).request(
        conn,
        workspace_id=source.workspace_id,
        project_id=source.repo.project_id,
        run_id=source.run_id,
        task_id=None,
        action_type="MANUAL_REVIEW",
        risk_level="high",
        subject=subject,
        requested_by_type="user" if requested_by else "system",
        requested_by=requested_by,
    )


async def request_pull_request(
    conn: AsyncConnection, source: PrSource, *, base_branch: str | None, title: str | None, actor_id: uuid.UUID
) -> dict[str, Any]:
    """Событие `github.pr.requested` (исполняет воркер через ledger) + аудит. Возвращает описание задания."""
    base = base_branch or source.repo.default_branch
    job = {
        "run_id": str(source.run_id),
        "task_id": str(source.task_id),
        "repository_id": str(source.repo.repository_id),
        "repository_full_name": source.repo.full_name,
        "head_branch": source.branch,
        "head_sha": source.head_sha,
        "base_branch": base,
        "title": (title or f"AiCodingDev: {source.objective}")[:256],
        "requested_by": str(actor_id),
    }
    await outbox.emit(
        conn,
        event_type=PR_REQUESTED_EVENT,
        aggregate_type="run",
        aggregate_id=source.run_id,
        workspace_id=source.workspace_id,
        run_id=source.run_id,
        payload=job,
    )
    await audit.record(
        conn,
        action="github.pr.request",
        resource_type="run",
        resource_id=source.run_id,
        actor_type="user",
        actor_id=actor_id,
        workspace_id=source.workspace_id,
        metadata={k: job[k] for k in ("repository_full_name", "head_branch", "head_sha", "base_branch")},
    )
    return job


def merge_subject(pr: Mapping[str, Any], method: str) -> dict[str, Any]:
    return {
        "scope": "github",
        "kind": "pr_merge",
        "pull_request_id": str(pr["id"]),
        "repository": str(pr["repository_full_name"]),
        "pr_number": int(pr["pr_number"]),
        "head_sha": str(pr["head_sha"]),
        "base_branch": str(pr["base_branch"]),
        "merge_method": method,
        "summary": f"Merge PR #{pr['pr_number']} в {pr['base_branch']} ({pr['repository_full_name']})",
    }


async def request_merge(conn: AsyncConnection, pr: Mapping[str, Any], *, actor_id: uuid.UUID, method: str) -> uuid.UUID:
    """Approval MERGE на конкретный предмет (head SHA). Повтор того же предмета — тот же PENDING."""
    if method not in MERGE_METHODS:
        raise ProblemError("validation_error", errors=[FieldError("/merge_method", "enum", "merge | squash | rebase")])
    if pr["state"] != "OPEN":
        raise ProblemError("invalid_state_transition", extensions={"current_status": pr["state"]})
    if pr["run_id"] is None:
        raise ProblemError("invalid_state_transition", detail="PR не связан с run платформы")
    approval_id = await Approvals().request(
        conn,
        workspace_id=pr["workspace_id"],
        project_id=pr["project_id"],
        run_id=pr["run_id"],
        task_id=None,
        action_type="MERGE",
        risk_level="high",
        subject=merge_subject(pr, method),
        requested_by_type="user",
        requested_by=actor_id,
    )
    await audit.record(
        conn,
        action="github.pr.merge_request",
        resource_type="pull_request",
        resource_id=pr["id"],
        actor_type="user",
        actor_id=actor_id,
        workspace_id=pr["workspace_id"],
        metadata={"approval_id": str(approval_id), "head_sha": pr["head_sha"], "merge_method": method},
    )
    return approval_id


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value is not None else None
    except ValueError:
        return None


class GitHubWorkflow:
    """Исполнитель внешних действий GitHub на воркере (installation token только в памяти процесса)."""

    def __init__(
        self,
        engine: AsyncEngine,
        client: GitHubClient,
        *,
        repository_roots: Mapping[str, Path],
        git_url: str,
        proxy_url: str | None = None,
        lease_ttl_seconds: int = LEASE_TTL_SECONDS,
    ) -> None:
        self.engine = engine
        self.client = client
        self.repository_roots = dict(repository_roots)
        self.git_url = git_url.rstrip("/")
        self.proxy_url = proxy_url
        self.lease_ttl_seconds = lease_ttl_seconds
        self.approvals = Approvals()

    # --- PR ---------------------------------------------------------------------------------------

    async def open_pull_request(self, ws: uuid.UUID, job: Mapping[str, Any]) -> Outcome:
        run_id, task_id = _uuid(job.get("run_id")), _uuid(job.get("task_id"))
        requested_by = _uuid(job.get("requested_by"))
        if run_id is None or task_id is None:
            return "ignored"
        ctx = DbContext(workspace_id=ws, actor_type="system")
        try:
            async with transaction(self.engine, ctx) as conn:
                source = await load_pr_source(conn, run_id, task_id)
        except ProblemError as exc:
            await self._audit(ws, "github.pr.rejected", "run", run_id, "failure", {"reason": exc.detail or exc.code})
            return "rejected"
        repo_path = repository_path(source.repo, self.repository_roots)
        paths = await asyncio.to_thread(changed_paths, repo_path, source.base_sha, source.head_sha)
        async with transaction(self.engine, ctx) as conn:
            pending = await require_ci_push_approval(conn, source, paths, requested_by=requested_by)
        if pending is not None:
            await self._audit(
                ws, "github.pr.blocked", "run", run_id, "denied", {"reason": "ci_config_change", "approval_id": pending}
            )
            return "blocked"
        base = str(job.get("base_branch") or source.repo.default_branch)
        title = str(job.get("title") or f"AiCodingDev: {source.objective}")[:256]
        target = f"{source.repo.full_name}#{source.branch}"
        common: dict[str, Any] = {"repository_id": str(source.repo.repository_id), "head_sha": source.head_sha}
        async with transaction(self.engine, ctx) as conn:
            push = await self._plan(conn, source, "git_push", target, {**common, "branch": source.branch})
            create = await self._plan(
                conn, source, "pr_create", target, {**common, "head": source.branch, "base": base, "title": title}
            )
            review = await self._plan(conn, source, "pr_comment", target + "#review", common)
            status = await self._plan(
                conn, source, "pr_update", f"{source.repo.full_name}@{source.head_sha}#status:{STATUS_CONTEXT}", common
            )
            lease = await leases.acquire(
                conn,
                resource_type="external_action",
                resource_id=create.id,
                worker_id=leases.worker_identity("github"),
                workspace_id=ws,
                ttl_seconds=self.lease_ttl_seconds,
            )
        if lease is None:
            raise RuntimeError("PR этого run уже создаёт другой воркер")  # повтор Dramatiq после освобождения
        try:
            pull = await self._push_and_create(ws, source, base, title, push.id, create.id, lease.fencing_token)
            async with transaction(self.engine, ctx) as conn:
                pr_id = await store.record_pull_request(
                    conn,
                    workspace_id=ws,
                    repository_id=source.repo.repository_id,
                    run_id=source.run_id,
                    create_action_id=create.id,
                    pull=pull,
                )
            await self._review_and_status(ws, source, pull, review.id, status.id, lease.fencing_token)
        finally:
            async with transaction(self.engine, ctx) as conn:
                await leases.release(conn, lease)
        await self._audit(
            ws, "github.pr.opened", "pull_request", pr_id, "success", {"pr_number": pull.number, "url": pull.url}
        )
        return "opened"

    async def _plan(
        self, conn: AsyncConnection, source: PrSource, action_type: str, target: str, request: dict[str, Any]
    ) -> external_actions.ExternalAction:
        return await external_actions.plan(
            conn,
            workspace_id=source.workspace_id,
            run_id=source.run_id,
            task_id=source.task_id,
            action_type=action_type,
            target=target,
            plan_version=None,
            request=request,
        )

    async def _token(self, binding: RepoBinding, operation: Any) -> InstallationToken:
        return await self.client.installation_token(
            binding.installation_id, operation, repository_ids=[binding.github_repo_id]
        )

    async def _push_and_create(
        self,
        ws: uuid.UUID,
        source: PrSource,
        base: str,
        title: str,
        push_id: uuid.UUID,
        create_id: uuid.UUID,
        fencing: int,
    ) -> PullInfo:
        repo, binding = source.repo.ref, source.repo
        repo_path = repository_path(binding, self.repository_roots)

        async def push_probe() -> external_actions.Outcome | None:
            branch = await self.client.branch(await self._token(binding, "read"), repo, source.branch)
            if branch is not None and branch.sha == source.head_sha:
                return external_actions.Outcome(f"{repo.full_name}@{source.head_sha}", {"branch": source.branch})
            return None

        async def push_do() -> external_actions.Outcome:
            token = await self._token(binding, "push")
            try:
                await asyncio.to_thread(
                    push_ref,
                    repo_path,
                    remote_url=f"{self.git_url}/{repo.owner}/{repo.name}.git",
                    sha=source.head_sha,
                    branch=source.branch,
                    token=token.value(),
                    proxy_url=self.proxy_url,
                )
            except GitRepositoryError as exc:
                raise RuntimeError(f"{exc}: {redact_text(exc.detail)}") from None
            return external_actions.Outcome(f"{repo.full_name}@{source.head_sha}", {"branch": source.branch})

        await external_actions.execute_once(self.engine, ws, push_id, fencing, do=push_do, probe=push_probe)
        marker = f"<!-- aicd:pr:{source.run_id}:{source.task_id} -->"
        found: list[PullInfo] = []

        async def existing() -> PullInfo | None:
            pulls = await self.client.find_pulls(await self._token(binding, "read"), repo, source.branch)
            return pulls[0] if pulls else None

        def outcome(pull: PullInfo) -> external_actions.Outcome:
            found.append(pull)
            return external_actions.Outcome(pull.url, {"number": pull.number, "head_sha": pull.head_sha})

        async def create_probe() -> external_actions.Outcome | None:
            pull = await existing()
            return outcome(pull) if pull is not None else None

        async def create_do() -> external_actions.Outcome:
            pull = await existing()  # собственная идемпотентность GitHub-стороны: PR из этой ветки уже есть
            if pull is None:
                pull = await self.client.create_pull(
                    await self._token(binding, "pr_create"),
                    repo,
                    title=title,
                    head=source.branch,
                    base=base,
                    body=self._pr_body(source, marker),
                )
            return outcome(pull)

        action = await external_actions.execute_once(
            self.engine, ws, create_id, fencing, do=create_do, probe=create_probe
        )
        if found:
            return found[-1]
        number = int((action.result or {}).get("number", 0))  # CONFIRMED ранее — состояние из GitHub
        return await self.client.pull(await self._token(binding, "read"), repo, number)

    def _pr_body(self, source: PrSource, marker: str) -> str:
        return (
            f"Изменения подготовлены AiCodingDev (run `{source.run_id}`, задача `{source.task_id}`).\n\n"
            f"Коммит `{source.head_sha}` прошёл обязательные проверки платформы. Merge выполняется только после"
            " явного approval `MERGE` и при соблюдении branch protection.\n\n" + marker
        )

    async def _review_and_status(
        self,
        ws: uuid.UUID,
        source: PrSource,
        pull: PullInfo,
        review_id: uuid.UUID,
        status_id: uuid.UUID,
        fencing: int,
    ) -> None:
        repo, binding = source.repo.ref, source.repo
        async with transaction(self.engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
            summary = await _gate_summary(conn, source.run_id)
        marker = f"<!-- aicd:review:{review_id} -->"

        async def review_probe() -> external_actions.Outcome | None:
            for item in await self.client.reviews(await self._token(binding, "read"), repo, pull.number):
                if marker in str(item.get("body") or ""):
                    return external_actions.Outcome(str(item.get("id")), {"pr_number": pull.number})
            return None

        async def review_do() -> external_actions.Outcome:
            body = _review_body(summary) + "\n\n" + marker
            rid = await self.client.create_review(
                await self._token(binding, "pr_comment"), repo, pull.number, body=body
            )
            return external_actions.Outcome(str(rid), {"pr_number": pull.number})

        await external_actions.execute_once(self.engine, ws, review_id, fencing, do=review_do, probe=review_probe)
        state = "success" if summary["failed"] == 0 else "failure"

        async def status_probe() -> external_actions.Outcome | None:
            statuses = await self.client.commit_statuses(await self._token(binding, "read"), repo, source.head_sha)
            if statuses.get(STATUS_CONTEXT) == state:
                return external_actions.Outcome(f"{repo.full_name}@{source.head_sha}", {"state": state})
            return None

        async def status_do() -> external_actions.Outcome:
            await self.client.set_status(
                await self._token(binding, "status"),
                repo,
                source.head_sha,
                state=state,
                context=STATUS_CONTEXT,
                description=f"AiCodingDev: {summary['passed']} gates PASS, {summary['failed']} FAIL",
            )
            return external_actions.Outcome(f"{repo.full_name}@{source.head_sha}", {"state": state})

        await external_actions.execute_once(self.engine, ws, status_id, fencing, do=status_do, probe=status_probe)

    # --- Merge ------------------------------------------------------------------------------------

    async def merge(self, ws: uuid.UUID, approval_id: uuid.UUID) -> Outcome:
        ctx = DbContext(workspace_id=ws, actor_type="system")
        async with transaction(self.engine, ctx) as conn:
            approval = await self.approvals.get(conn, approval_id)
            if approval is None or approval["action_type"] != "MERGE":
                return "ignored"
            subject = approval["subject"] if isinstance(approval["subject"], dict) else {}
            if subject.get("scope") != "github" or subject.get("kind") != "pr_merge":
                return "ignored"
            if approval["status"] != "APPROVED":
                return "denied"
            pr_id = _uuid(subject.get("pull_request_id"))
            pr = await store.get_pull_request(conn, pr_id) if pr_id else None
            if pr is None:
                return "ignored"
            binding = await load_binding(conn, pr["repository_id"])
            action = await external_actions.plan(
                conn,
                workspace_id=ws,
                run_id=approval["run_id"],
                task_id=None,
                action_type="pr_merge",
                target=f"{binding.full_name}#{pr['pr_number']}@{approval_id}",
                plan_version=None,
                request=subject,
                approval_id=approval_id,
            )
            if action.status == "CONFIRMED":
                return "merged"
            lease = await leases.acquire(
                conn,
                resource_type="external_action",
                resource_id=action.id,
                worker_id=leases.worker_identity("github"),
                workspace_id=ws,
                ttl_seconds=self.lease_ttl_seconds,
            )
        if lease is None:
            raise RuntimeError("merge этого PR уже выполняет другой воркер")
        repo, number = binding.ref, int(pr["pr_number"])
        head_sha, method = str(subject["head_sha"]), str(subject.get("merge_method") or "merge")
        try:

            async def probe() -> external_actions.Outcome | None:
                pull = await self.client.pull(await self._token(binding, "read"), repo, number)
                if pull.merged and pull.merge_commit_sha:
                    return external_actions.Outcome(f"{repo.full_name}#{number}", {"merged_sha": pull.merge_commit_sha})
                return None

            async def do() -> external_actions.Outcome:
                token = await self._token(binding, "merge")
                await self._check_mergeable(token, repo, number, head_sha, str(subject.get("base_branch") or ""))
                merged_sha = await self.client.merge(
                    token, repo, number, sha=head_sha, method=method, title=f"Merge PR #{number} (AiCodingDev)"
                )
                return external_actions.Outcome(f"{repo.full_name}#{number}", {"merged_sha": merged_sha})

            try:
                await external_actions.execute_once(self.engine, ws, action.id, lease.fencing_token, do=do, probe=probe)
            except external_actions.ApprovalRequiredError:
                await self._audit(ws, "github.pr.merge_denied", "pull_request", pr["id"], "denied", {})
                return "denied"
            except (MergeBlockedError, GitHubError) as exc:
                await self._audit(
                    ws, "github.pr.merge_blocked", "pull_request", pr["id"], "failure", {"reason": str(exc)[:500]}
                )
                return "blocked"
            pull = await self.client.pull(await self._token(binding, "read"), repo, number)
            async with transaction(self.engine, ctx) as conn:
                await store.apply_remote_state(conn, pr["id"], pull, merge_action_id=action.id)
        finally:
            async with transaction(self.engine, ctx) as conn:
                await leases.release(conn, lease)
        await self._audit(
            ws, "github.pr.merged", "pull_request", pr["id"], "success", {"approval_id": approval_id, "pr": number}
        )
        return "merged"

    async def _check_mergeable(
        self, token: InstallationToken, repo: RepoRef, number: int, head_sha: str, base_branch: str
    ) -> None:
        """Состояние из GitHub API (не из вебхука): PR открыт, head = утверждённому SHA, защита ветки выполнена."""
        pull = await self.client.pull(token, repo, number)
        if pull.merged or pull.state != "open":
            raise MergeBlockedError(f"PR #{number} не открыт (state={pull.state})")
        if pull.head_sha != head_sha:
            raise MergeBlockedError("head PR изменился после approval — нужен новый approval MERGE")
        if base_branch and pull.base_ref != base_branch:
            raise MergeBlockedError("базовая ветка PR изменилась после approval")
        branch = await self.client.branch(token, repo, pull.base_ref)
        if branch is not None and branch.protected and branch.required_contexts:
            statuses = await self.client.commit_statuses(token, repo, head_sha)
            checks = await self.client.check_runs(token, repo, head_sha)
            missing = [
                c for c in branch.required_contexts if statuses.get(c) != "success" and checks.get(c) not in CHECK_OK
            ]
            if missing:
                raise MergeBlockedError(f"обязательные проверки не пройдены: {', '.join(missing)[:300]}")
        if pull.mergeable is not True or (pull.mergeable_state or "unknown") not in MERGEABLE_STATES:
            raise MergeBlockedError(f"GitHub не разрешает merge (mergeable_state={pull.mergeable_state})")

    # --- Вебхуки ------------------------------------------------------------------------------------

    async def process_webhook(self, event: Mapping[str, Any]) -> Outcome:
        payload = event.get("payload") or {}
        delivery_id = _uuid(payload.get("delivery_id"))
        if delivery_id is None:
            return "ignored"
        system = DbContext(actor_type="system")
        async with transaction(self.engine, system) as conn:
            delivery = await store.get_delivery(conn, delivery_id)
            if delivery is None or delivery["status"] != "RECEIVED" or not delivery["signature_valid"]:
                return "duplicate"
            installation_id = delivery["installation_id"]
            ws = await store.resolve_installation_workspace(conn, installation_id) if installation_id else None
        if ws is None:
            await self._finish(delivery_id, "IGNORED", "unknown installation")
            return "ignored"
        raw_hint = payload.get("hint")
        hint: dict[str, Any] = dict(raw_hint) if isinstance(raw_hint, dict) else {}
        try:
            outcome = await self._apply_webhook(ws, str(delivery["event"]), int(installation_id), hint)
        except GitHubError as exc:
            if exc.status == 0 or exc.status >= 500:
                raise  # временная ошибка GitHub — повтор Dramatiq (доставка остаётся RECEIVED)
            await self._finish(delivery_id, "FAILED", str(exc))
            return "failed"
        await self._finish(delivery_id, "PROCESSED" if outcome == "processed" else "IGNORED", None)
        return outcome

    async def _apply_webhook(self, ws: uuid.UUID, event: str, installation_id: int, hint: dict[str, Any]) -> Outcome:
        ctx = DbContext(workspace_id=ws, actor_type="system")
        if event == "installation":
            try:
                remote = await self.client.installation(installation_id)
                status = "SUSPENDED" if remote.suspended else "ACTIVE"
            except GitHubError as exc:
                if exc.status != 404:
                    raise
                status = "REVOKED"
            async with transaction(self.engine, ctx) as conn:
                inst = await store.installation_by_github_id(conn, installation_id)
                if inst is None:
                    return "ignored"
                # Отключённую пользователем установку (REVOKED) вебхук не реактивирует.
                if inst["status"] != status and inst["status"] != "REVOKED":
                    await store.set_installation_status(conn, inst["id"], status)
            return "processed"
        if event in ("pull_request", "pull_request_review"):
            repo_id, number = hint.get("repository_id"), hint.get("pr_number")
            if not isinstance(repo_id, int) or not isinstance(number, int):
                return "ignored"
            async with transaction(self.engine, ctx) as conn:
                row = (
                    (
                        await conn.execute(
                            text(
                                "SELECT p.id, p.repository_id FROM pull_requests p JOIN repositories r"
                                " ON r.id = p.repository_id WHERE r.github_repo_id = :g AND p.pr_number = :n"
                            ),
                            {"g": repo_id, "n": number},
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    return "ignored"
                binding = await load_binding(conn, row["repository_id"])
            pull = await self.client.pull(await self._token(binding, "read"), binding.ref, number)
            async with transaction(self.engine, ctx) as conn:
                await store.apply_remote_state(conn, row["id"], pull)
            return "processed"
        return "ignored"

    async def _finish(self, delivery_id: uuid.UUID, status: str, error: str | None) -> None:
        async with transaction(self.engine, DbContext(actor_type="system")) as conn:
            await store.finish_delivery(conn, delivery_id, status, redact_text(error) if error else None)

    async def _audit(
        self,
        ws: uuid.UUID,
        action: str,
        resource_type: str,
        resource_id: uuid.UUID,
        outcome: str,
        metadata: dict[str, Any],
    ) -> None:
        async with transaction(self.engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
            await audit.record(
                conn,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                actor_type="system",
                actor_id=None,
                workspace_id=ws,
                outcome=outcome,
                metadata=json.loads(json.dumps(metadata, default=str)),
            )


async def _gate_summary(conn: AsyncConnection, run_id: uuid.UUID) -> dict[str, Any]:
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT gate_type, status, count(*) AS n FROM gate_executions WHERE run_id = :r"
                    " AND superseded_at IS NULL GROUP BY gate_type, status ORDER BY gate_type, status"
                ),
                {"r": run_id},
            )
        )
        .mappings()
        .all()
    )
    passed = sum(int(r["n"]) for r in rows if r["status"] in ("PASS", "NOT_APPLICABLE"))
    failed = sum(int(r["n"]) for r in rows if r["status"] in ("FAIL", "ERROR"))
    return {"rows": [dict(r) for r in rows], "passed": passed, "failed": failed}


def _review_body(summary: Mapping[str, Any]) -> str:
    lines = ["### AiCodingDev: итоги проверок", "", "| Gate | Статус | Кол-во |", "|---|---|---|"]
    lines += [f"| {r['gate_type']} | {r['status']} | {r['n']} |" for r in summary["rows"]]
    lines += ["", "Ревью носит информационный характер: PR не одобряется автоматически."]
    return "\n".join(lines)
