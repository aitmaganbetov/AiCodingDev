"""CLI-провайдеры (ТЗ §8, API_DESIGN §15): health и capabilities без учётных данных, перепроверка установки.

* `GET /providers` — установки и health (без токенов, путей, env);
* `POST /providers/{id}/probe` — асинхронная перепроверка на worker-exec (202 `ProbeAccepted`); API не выполняет
  login и не принимает credentials; лимит 6/час на workspace+провайдера (API_DESIGN §10);
* `GET /providers/{id}/probes/{probe_request_id}` — статус запроса (`ProbeAccepted.status_url`, OQ-45 (c)).

* `PATCH /providers/{id}/settings` — безопасные настройки workspace (`policies`, policy_type=provider_settings),
  If-Match = `settings_version`, аудит `provider.settings.update`.

Интерактивный вход по подписке из UI (device code, P2-01, SECURITY_ARCHITECTURE §6.2):

* `POST /providers/{id}/login` — 202 `LoginAccepted`: worker-exec запускает официальную команду входа CLI в PTY
  на хосте исполнения. API **не** принимает и не хранит токены (ТЗ §1, cli-auth.md §0): наружу выходит только
  публичная часть device-code потока — ссылка и код, которые оператор вводит на сайте провайдера;
* `GET /providers/{id}/logins/{login_session_id}` — состояние сессии (`LoginAccepted.status_url`);
* `POST /providers/{id}/logins/{login_session_id}/cancel` — отмена: воркер снимает процесс CLI;
* `POST /providers/{id}/logins/{login_session_id}/code` — код авторизации из браузера оператора для тех CLI,
  которые завершают вход его вводом (Claude Code). Код идёт в stdin процесса CLI через pub/sub и нигде не
  сохраняется: ни в БД, ни в audit, ни в логах, ни в ответе. Обменять его может только CLI (PKCE).
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import Response

from aicd_api.deps import (
    _remember_rate,
    app_state,
    authorize,
    current_principal,
    parse_workspace_header,
    require_step_up,
    workspace_access,
)
from aicd_api.idempotency import CommandResult, run_idempotent
from aicd_api.responses import etag, json_response, require_if_match
from aicd_api.routers import op
from aicd_api.schemas import ProviderLoginCodeRequest, ProviderSettingsUpdateRequest
from aicd_cli_adapters import limits as cli_limits
from aicd_cli_adapters import login_registry, registry
from aicd_identity import api as identity
from aicd_platform import login_input
from aicd_platform.clock import utcnow
from aicd_platform.db import transaction
from aicd_platform.errors import FieldError, ProblemError

router = APIRouter(tags=["providers"])

PROBES_PER_HOUR = 6
#: Обновление лимитов подписки задаёт CLI один запрос к модели — отдельный и более строгий по смыслу лимит.
LIMIT_REFRESHES_PER_HOUR = 12
log = logging.getLogger("aicd.providers")
#: Попытки входа ограничены жёстче probe: device code живёт минутами, частые попытки — признак проблемы.
LOGINS_PER_HOUR = 10
#: Ввод кода в активную сессию: с запасом на опечатки оператора, но не канал для записи в stdin CLI.
LOGIN_CODES_PER_HOUR = 20


def status_url(provider_id: uuid.UUID, probe_request_id: uuid.UUID) -> str:
    return f"/api/v1/providers/{provider_id}/probes/{probe_request_id}"


def login_status_url(provider_id: uuid.UUID, login_session_id: uuid.UUID) -> str:
    return f"/api/v1/providers/{provider_id}/logins/{login_session_id}"


@router.get("/providers", **op("listProviders", "provider:read"))
async def list_providers(request: Request, principal: identity.Principal = Depends(current_principal)) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "provider:read")
    state = app_state(request)

    async def limits_of(installation_id: uuid.UUID) -> dict[str, Any] | None:
        # Наблюдение — подсказка: недоступный Redis не должен ломать список провайдеров.
        try:
            return await cli_limits.load(
                state.redis, environment=state.settings.environment, installation_id=installation_id
            )
        except Exception:
            log.warning("subscription limits are unavailable", exc_info=True)
            return None

    async with transaction(state.engine, access.db()) as conn:
        items = await registry.list_providers(conn, limits_of=limits_of)
    return json_response({"items": items})


@router.post("/providers/{provider_id}/probe", status_code=202, **op("probeProvider", "provider:probe"))
async def probe_provider(
    request: Request,
    provider_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "provider:probe")
    state = app_state(request)
    async with transaction(state.engine, access.db()) as conn:
        code = await registry.provider_code(conn, provider_id)
    if code is None:
        raise ProblemError("not_found")
    _remember_rate(
        request,
        await state.limiter.hit(
            f"probe:ws:{access.workspace_id}:provider:{provider_id}", PROBES_PER_HOUR, window_seconds=3600
        ),
    )

    async def command(conn: AsyncConnection) -> CommandResult:
        probe_request_id, accepted_at = await registry.request_probe(
            conn, workspace_id=access.workspace_id, provider_id=provider_id, provider=code, actor_id=principal.user_id
        )
        url = status_url(provider_id, probe_request_id)
        body = {
            "provider_id": provider_id,
            "probe_request_id": probe_request_id,
            "accepted_at": accepted_at,
            "status_url": url,
        }
        return CommandResult(202, body, {})

    return await run_idempotent(request, access.db(), {"provider_id": str(provider_id)}, command)


@router.post(
    "/providers/{provider_id}/limits/refresh", status_code=202, **op("refreshProviderLimits", "provider:probe")
)
async def refresh_provider_limits(
    request: Request,
    provider_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    """Обновить лимиты подписки: проверка установки плюс чтение лимитов у самого CLI.

    Лимиты сообщает только сам CLI (`aicd_cli_adapters.limits`); API к провайдеру не обращается. У Claude Code
    чтение — один минимальный запрос к модели (расходует подписку), поэтому запросы ограничены отдельно от
    обычной проверки; у Codex оно бесплатно (`codex app-server`).
    """
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "provider:probe")
    state = app_state(request)
    async with transaction(state.engine, access.db()) as conn:
        code = await registry.provider_code(conn, provider_id)
    if code is None:
        raise ProblemError("not_found")
    if code not in cli_limits.READABLE:
        raise ProblemError(
            "validation_error",
            errors=[FieldError("/provider_id", "unsupported", "CLI этого провайдера не сообщает лимиты подписки")],
        )
    _remember_rate(
        request,
        await state.limiter.hit(
            f"limits:ws:{access.workspace_id}:provider:{provider_id}", LIMIT_REFRESHES_PER_HOUR, window_seconds=3600
        ),
    )

    async def command(conn: AsyncConnection) -> CommandResult:
        probe_request_id, accepted_at = await registry.request_probe(
            conn,
            workspace_id=access.workspace_id,
            provider_id=provider_id,
            provider=code,
            actor_id=principal.user_id,
            with_limits=True,
        )
        body = {
            "provider_id": provider_id,
            "probe_request_id": probe_request_id,
            "accepted_at": accepted_at,
            "status_url": status_url(provider_id, probe_request_id),
        }
        return CommandResult(202, body, {})

    return await run_idempotent(request, access.db(), {"provider_id": str(provider_id), "limits": True}, command)


@router.get("/providers/{provider_id}/probes/{probe_request_id}", **op("getProviderProbe", "provider:read"))
async def get_provider_probe(
    request: Request,
    provider_id: Annotated[uuid.UUID, Path()],
    probe_request_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "provider:read")
    async with transaction(app_state(request).engine, access.db()) as conn:
        body = await registry.probe_status(
            conn, provider_id=provider_id, probe_request_id=probe_request_id, now=utcnow()
        )
    if body is None:
        raise ProblemError("not_found")
    return json_response(body)


@router.post("/providers/{provider_id}/login", status_code=202, **op("loginProvider", "provider:login"))
async def login_provider(
    request: Request,
    provider_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    """Начать интерактивный вход CLI по подписке на хосте исполнения (device code).

    Платформа не выполняет вход за оператора и не получает учётных данных: worker-exec запускает официальную
    команду CLI, а ответ и статус содержат только ссылку и код, которые оператор вводит сам на сайте
    провайдера (ADR-0003, cli-auth.md §0). Owner + step-up: вход привязывает подписку к хосту (OQ-01).
    """
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "provider:login")
    await require_step_up(request, principal, "provider:login")
    state = app_state(request)
    async with transaction(state.engine, access.db()) as conn:
        code = await registry.provider_code(conn, provider_id)
    if code is None:
        raise ProblemError("not_found")
    _remember_rate(
        request,
        await state.limiter.hit(
            f"login:ws:{access.workspace_id}:provider:{provider_id}", LOGINS_PER_HOUR, window_seconds=3600
        ),
    )

    async def command(conn: AsyncConnection) -> CommandResult:
        session_id, accepted_at = await login_registry.request_login(
            conn, workspace_id=access.workspace_id, provider_id=provider_id, provider=code, actor_id=principal.user_id
        )
        body = {
            "provider_id": provider_id,
            "login_session_id": session_id,
            "accepted_at": accepted_at,
            "status_url": login_status_url(provider_id, session_id),
        }
        return CommandResult(202, body, {})

    return await run_idempotent(request, access.db(), {"provider_id": str(provider_id)}, command)


@router.get("/providers/{provider_id}/logins/{login_session_id}", **op("getProviderLogin", "provider:login"))
async def get_provider_login(
    request: Request,
    provider_id: Annotated[uuid.UUID, Path()],
    login_session_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    """Состояние сессии входа: ссылка и код устройства, пока вход идёт, и итог после завершения."""
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "provider:login")
    async with transaction(app_state(request).engine, access.db()) as conn:
        body = await login_registry.login_status(conn, provider_id=provider_id, session_id=login_session_id)
    if body is None:
        raise ProblemError("not_found")
    return json_response(body)


@router.post(
    "/providers/{provider_id}/logins/{login_session_id}/cancel",
    status_code=202,
    **op("cancelProviderLogin", "provider:login"),
)
async def cancel_provider_login(
    request: Request,
    provider_id: Annotated[uuid.UUID, Path()],
    login_session_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    """Отменить сессию входа. Уже завершённая сессия — 202 с её текущим состоянием (идемпотентно)."""
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "provider:login")

    async def command(conn: AsyncConnection) -> CommandResult:
        current = await login_registry.login_status(conn, provider_id=provider_id, session_id=login_session_id)
        if current is None:
            raise ProblemError("not_found")
        await login_registry.cancel_session(
            conn,
            workspace_id=access.workspace_id,
            provider_id=provider_id,
            session_id=login_session_id,
            actor_id=principal.user_id,
        )
        body = await login_registry.login_status(conn, provider_id=provider_id, session_id=login_session_id)
        return CommandResult(202, body, {})

    return await run_idempotent(
        request, access.db(), {"login_session_id": str(login_session_id), "cancel": True}, command
    )


@router.post(
    "/providers/{provider_id}/logins/{login_session_id}/code",
    status_code=202,
    **op("submitProviderLoginCode", "provider:login"),
)
async def submit_provider_login_code(
    request: Request,
    provider_id: Annotated[uuid.UUID, Path()],
    login_session_id: Annotated[uuid.UUID, Path()],
    body: ProviderLoginCodeRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    """Передать процессу входа CLI код авторизации, который провайдер показал оператору в браузере.

    Платформа код **не хранит**: он не попадает ни в БД, ни в audit (там только факт), ни в логи, ни в ответ —
    идёт через pub/sub в stdin CLI на хосте исполнения (`aicd_platform.login_input`). Это не токен: обменять
    одноразовый код OAuth/PKCE может только сам CLI, `code_verifier` платформе неизвестен (ТЗ §1, ADR-0003).
    """
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "provider:login")
    try:
        code = login_input.normalize(body.code)
    except ValueError as exc:
        raise ProblemError("validation_error", errors=[FieldError("/code", "format", str(exc))]) from exc
    _remember_rate(
        request,
        await app_state(request).limiter.hit(
            f"login-code:session:{login_session_id}", LOGIN_CODES_PER_HOUR, window_seconds=3600
        ),
    )

    async def command(conn: AsyncConnection) -> CommandResult:
        current = await login_registry.login_status(conn, provider_id=provider_id, session_id=login_session_id)
        if current is None:
            raise ProblemError("not_found")
        if current["state"] not in login_registry.ACTIVE_STATES:
            raise ProblemError("conflict", detail="сессия входа уже завершена: код передавать некому")
        # Получателей нет — процесс входа идёт на другом хосте или уже закончился; код никуда не сохраняется.
        if await login_input.publish(app_state(request).redis, session_id=login_session_id, code=code) == 0:
            raise ProblemError("conflict", detail="процесс входа CLI не слушает ввод: повторите вход")
        await login_registry.record_code_submission(
            conn,
            workspace_id=access.workspace_id,
            provider_id=provider_id,
            session_id=login_session_id,
            actor_id=principal.user_id,
        )
        return CommandResult(202, current, {})

    return await run_idempotent(
        request, access.db(), {"login_session_id": str(login_session_id), "code_submitted": True}, command
    )


@router.patch("/providers/{provider_id}/settings", **op("updateProviderSettings", "provider:settings"))
async def update_provider_settings(
    request: Request,
    provider_id: Annotated[uuid.UUID, Path()],
    body: ProviderSettingsUpdateRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "provider:settings")
    await require_step_up(request, principal, "provider:settings")  # SC-IAM-08: политика провайдера в workspace
    async with transaction(app_state(request).engine, access.db()) as conn:
        if await registry.provider_code(conn, provider_id) is None:
            raise ProblemError("not_found")
    expected = require_if_match(request.headers.get("if-match"))
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise ProblemError("validation_error", errors=[FieldError("/", "min_properties", "нет изменяемых полей")])
    for key, value in changes.items():
        # `model: null` — осмысленное значение («модель по умолчанию CLI»); у остальных полей null нет.
        if value is None and key != "model":
            raise ProblemError("validation_error", errors=[FieldError(f"/{key}", "null", "значение обязательно")])
    roles = changes.get("allowed_roles")
    if roles is not None and len(set(roles)) != len(roles):
        raise ProblemError(
            "validation_error", errors=[FieldError("/allowed_roles", "unique_items", "роли не должны повторяться")]
        )
    async with transaction(app_state(request).engine, access.db()) as conn:
        view = await registry.update_workspace_settings(
            conn,
            workspace_id=access.workspace_id,
            provider_id=provider_id,
            actor_id=principal.user_id,
            expected_version=expected,
            changes=changes,
        )
    return json_response(view, headers={"ETag": etag(view["settings_version"])})
