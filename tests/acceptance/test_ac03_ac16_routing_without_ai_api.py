"""AC-03 и AC-16 (P9-01) на Fake CLI с журналом исходящих соединений процесса платформы.

AC-03: Router назначает доступный официальный CLI (provider ∈ {codex, claude}) с `score_breakdown`;
       процесс платформы не обращается к AI API (egress-лог: только локальные БД/Redis/артефакты).
AC-16: оба провайдера `AUTH_REQUIRED` (или один, а другой не подходит по policy) → задача
       `BLOCKED(AUTH_REQUIRED | PROVIDER_UNAVAILABLE)`, причина видна в API, попыток исполнения
       (обходной авторизации) нет, обращений к AI API нет.

Сеть процесса агента (runner → эндпоинты провайдера через egress-proxy allowlist) проверяется отдельно:
tests/security/test_sandbox.py::test_egress_proxy_enforces_allowlist; отсутствие AI SDK — deny-gate
(tests/unit/test_ci_gates.py) и import-linter (tests/contract/test_architecture.py::test_import_contracts).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from aicd_platform.db import DbContext, transaction
from tests.acceptance.support import record_egress, rows
from tests.support.contract import validate_response
from tests.support.phase3 import Slice, create_run, drive, install_both, install_cli, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db


async def _patch_provider(s: Slice, code: str, body: dict[str, Any]) -> None:
    listed = (await s.user.client.get("/api/v1/providers", headers={"X-Workspace-Id": s.ws})).json()["items"]
    p = next(x for x in listed if x["code"] == code)
    version = p["workspace_settings"]["settings_version"]
    r = await s.user.client.patch(
        f"/api/v1/providers/{p['provider_id']}/settings",
        json=body,
        headers=s.user.headers(s.ws, if_match=f'"v{version}"'),
    )
    assert r.status_code == 200, r.text


async def test_ac03_router_selects_official_cli_and_platform_has_no_ai_api_egress(
    world: Slice, monkeypatch: pytest.MonkeyPatch
) -> None:
    await install_both(world.worker)
    with record_egress(monkeypatch) as egress:
        run_id = await create_run(world)
        assert await drive(world, run_id) == "COMPLETED"
        r = await world.user.client.get(f"/api/v1/runs/{run_id}")
        validate_response("getRun", r)
    assert egress.ai_api() == [], "обращения к AI API из процесса платформы"
    assert egress.remote() == [], f"исходящие соединения вне loopback: {egress.remote()}"
    # Перехват проверен самопроверкой при входе (record_egress → EgressLog.selfcheck).

    decisions = await rows(
        world,
        "SELECT t.agent_role, d.outcome, d.selected_provider, d.score_breakdown FROM routing_decisions d"
        " JOIN tasks t ON t.id = d.task_id WHERE t.run_id = :r ORDER BY d.created_at",
        r=uuid.UUID(run_id),
    )
    assert decisions and {d["agent_role"] for d in decisions} == {"developer", "reviewer"}
    for d in decisions:
        assert d["outcome"] == "SELECTED" and d["selected_provider"] in {"codex", "claude"}
        breakdown = d["score_breakdown"]
        assert {"weights", "candidates", "excluded"} <= set(breakdown), breakdown
        selected = [c for c in breakdown["candidates"] if c["provider"] == d["selected_provider"]]
        assert selected, "выбранный провайдер — один из оценённых кандидатов"
    attempts = await rows(
        world,
        "SELECT a.provider, e.command_profile FROM task_attempts a JOIN tasks t ON t.id = a.task_id"
        " JOIN cli_executions e ON e.task_attempt_id = a.id WHERE t.run_id = :r",
        r=uuid.UUID(run_id),
    )
    assert attempts and all(a["command_profile"].startswith(f"{a['provider']}.") for a in attempts), (
        "исполнение — через профиль команды официального CLI выбранного провайдера"
    )


async def _blocked_case(world: Slice, case: str) -> None:
    async with transaction(world.worker, DbContext(actor_type="system")) as conn:
        await conn.execute(text("UPDATE cli_installations SET enabled = false WHERE enabled"))
    if case == "both_auth_required":
        await install_cli(world.worker, provider="codex", auth_status="AUTH_REQUIRED", isolate=False)
        await install_cli(world.worker, provider="claude", auth_status="AUTH_REQUIRED", isolate=False)
    else:  # claude без авторизации, codex авторизован, но политикой workspace не допущен к роли developer
        await install_cli(world.worker, provider="claude", auth_status="AUTH_REQUIRED", isolate=False)
        await install_cli(world.worker, provider="codex", isolate=False)
        await _patch_provider(world, "codex", {"allowed_roles": ["reviewer"]})


@pytest.mark.parametrize(
    ("case", "reasons"),
    [
        ("both_auth_required", {"AUTH_REQUIRED"}),
        ("one_auth_required_other_denied_by_policy", {"AUTH_REQUIRED", "PROVIDER_UNAVAILABLE"}),
    ],
)
async def test_ac16_no_authorized_cli_blocks_task_with_visible_reason_and_no_workaround(
    world: Slice, monkeypatch: pytest.MonkeyPatch, case: str, reasons: set[str]
) -> None:
    await _blocked_case(world, case)
    with record_egress(monkeypatch) as egress:
        run_id = await create_run(world)
        assert await drive(world, run_id) == "BLOCKED"
        run = await world.user.client.get(f"/api/v1/runs/{run_id}")
        validate_response("getRun", run)
        tasks = await world.user.client.get(f"/api/v1/runs/{run_id}/tasks")
        validate_response("getRunTasks", tasks)
    assert egress.ai_api() == [] and egress.remote() == [], egress.remote()

    assert run.json()["status"] == "BLOCKED" and run.json()["blocked_reason"] in reasons
    blocked = [t for t in tasks.json()["tasks"] if t["status"] == "BLOCKED"]
    assert blocked, tasks.json()
    assert {t["blocked_reason"] for t in blocked} <= reasons, "причина блокировки видна в API задач"
    executions = await rows(
        world,
        "SELECT (SELECT count(*) FROM task_attempts a JOIN tasks t ON t.id = a.task_id WHERE t.run_id = :r) AS attempts,"
        " (SELECT count(*) FROM cli_executions e JOIN task_attempts a ON a.id = e.task_attempt_id"
        "  JOIN tasks t ON t.id = a.task_id WHERE t.run_id = :r) AS executions",
        r=uuid.UUID(run_id),
    )
    assert executions == [{"attempts": 0, "executions": 0}], "нет попыток запуска CLI (и обходной авторизации)"
    decisions = await rows(
        world,
        "SELECT d.outcome, d.score_breakdown FROM routing_decisions d JOIN tasks t ON t.id = d.task_id"
        " WHERE t.run_id = :r",
        r=uuid.UUID(run_id),
    )
    assert decisions and all(d["outcome"] == "BLOCKED" for d in decisions)
    excluded = {e["reason"] for d in decisions for e in d["score_breakdown"]["excluded"]} - {"disabled"}
    expected = {"auth_required"} if case == "both_auth_required" else {"auth_required", "role_not_allowed"}
    assert excluded == expected, excluded
