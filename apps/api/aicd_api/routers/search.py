"""Глобальный поиск по workspace (B6, UI_REDESIGN_PLAN §3.10 G-12): `GET /search`.

Сущности workspace из `X-Workspace-Id`, которые пользователь вправе читать (право — на каждый тип отдельно):

| тип | поля поиска | право |
|---|---|---|
| `project` | name, description | project:read |
| `run` | objective | run:read |
| `task` | title | run:read |
| `finding` | title, rule_id, cwe_id | quality:read |
| `approval` | action_type, decision_reason | approval:read |
| `documentation_source` | uri_or_file_ref | documentation:read |
| `chat_message` | body — FTS `chat_messages.search_vector` (`ix_chat_messages_search`) | chat:read |

Текстовые поля сопоставляются `ILIKE '%q%'` (wildcard экранируются, всё — bind-параметры) с ранжированием:
точное совпадение (1.0) > префикс (0.8 + сходство) > подстрока (0.5 + 0.3 × pg_trgm `similarity`); UUID в запросе —
точное совпадение id. Индексы: GIN `(workspace_id, … gin_trgm_ops)` по `runs.objective`, `tasks.title` и
выражению находки (миграция 0018); проекты, approvals, источники документации — малые таблицы. Каждый запрос ограничен workspace (RLS + явный фильтр `workspace_id`) и `limit`; транзакция —
`statement_timeout` 5 с. Отдельный rate limit на пользователя (`rate_limit_search_per_minute`, API_DESIGN §10),
минимальная длина запроса — 2 символа без пробелов по краям. Фрагмент (`snippet`) — окно вокруг совпадения,
секреты редактируются (`redact_text`). Подсказки маршрута — `api_path` и идентификаторы контекста (project/run/task).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import Response

from aicd_api.deps import app_state, authorize, current_principal, parse_workspace_header, workspace_access
from aicd_api.responses import json_response
from aicd_api.routers import op
from aicd_identity import api as identity
from aicd_identity.rbac import has_permission
from aicd_platform.db import transaction
from aicd_platform.errors import FieldError, ProblemError
from aicd_platform.redaction import redact_text

router = APIRouter(tags=["search"])

SearchType = Literal["project", "run", "task", "finding", "approval", "documentation_source", "chat_message"]
TYPE_ORDER: tuple[str, ...] = (
    "project", "run", "task", "finding", "approval", "documentation_source", "chat_message",
)  # fmt: skip
TYPE_PERMISSION = {
    "project": "project:read",
    "run": "run:read",
    "task": "run:read",
    "finding": "quality:read",
    "approval": "approval:read",
    "documentation_source": "documentation:read",
    "chat_message": "chat:read",
}
MIN_QUERY_CHARS = 2
MAX_LIMIT = 50
DEFAULT_LIMIT = 20
SNIPPET_CHARS = 160
TITLE_CHARS = 200
# То же выражение, что у индекса ix_findings_search_trgm (миграция 0018): иначе индекс не применяется.
_FINDING_TEXT = "(f.title || ' ' || f.rule_id || ' ' || coalesce(f.cwe_id, ''))"


@dataclass(frozen=True)
class _Query:
    raw: str
    contains: str
    prefix: str
    as_uuid: uuid.UUID | None


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _one_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def make_snippet(source: str | None, query: str, width: int = SNIPPET_CHARS) -> str | None:
    """Окно вокруг первого вхождения запроса (без учёта регистра) в одну строку, секреты редактируются."""
    if not source:
        return None
    flat = _one_line(redact_text(source))
    if not flat:
        return None
    pos = flat.lower().find(query.lower())
    if pos < 0:
        for word in query.split():
            pos = flat.lower().find(word.lower())
            if pos >= 0:
                break
    start = max(0, pos - width // 3) if pos >= 0 else 0
    end = min(len(flat), start + width)
    out = flat[start:end]
    return ("…" if start > 0 else "") + out + ("…" if end < len(flat) else "")


def _title(value: str | None) -> str:
    flat = _one_line(redact_text(value or ""))
    return flat[:TITLE_CHARS] + ("…" if len(flat) > TITLE_CHARS else "") if flat else "—"


_SCORE = (
    "CASE WHEN lower({col}) = lower(:q) THEN 1.0"
    " WHEN {col} ILIKE :prefix ESCAPE '\\' THEN 0.8 + 0.1 * similarity({col}, :q)"
    " ELSE 0.5 + 0.3 * similarity({col}, :q) END"
)


def _score(col: str) -> str:
    return _SCORE.format(col=col)


def _match(cols: list[str], q: _Query) -> str:
    if q.as_uuid is not None:
        return "id = :uid"
    return "(" + " OR ".join(f"{c} ILIKE :contains ESCAPE '\\'" for c in cols) + ")"


async def _rows(conn: AsyncConnection, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in (await conn.execute(text(sql), params)).mappings()]


def _item(
    kind: str,
    rid: uuid.UUID,
    title: str,
    snippet: str | None,
    score: float,
    created_at: datetime,
    *,
    project_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    api_path: str,
) -> dict[str, Any]:
    return {
        "type": kind,
        "id": rid,
        "title": title,
        "snippet": snippet,
        "score": round(float(score), 4),
        "created_at": created_at,
        "project_id": project_id,
        "run_id": run_id,
        "task_id": task_id,
        "api_path": api_path,
    }


async def _projects(conn: AsyncConnection, q: _Query, p: dict[str, Any]) -> list[dict[str, Any]]:
    score = (
        "1.0"
        if q.as_uuid
        else f"GREATEST({_score('name')}, CASE WHEN description ILIKE :contains ESCAPE '\\' THEN 0.5 ELSE 0 END)"
    )
    rows = await _rows(
        conn,
        f"SELECT id, name, description, created_at, {score} AS score FROM projects"  # noqa: S608 — подставляются только константы модуля
        f" WHERE workspace_id = :ws AND {_match(['name', 'description'], q)}"
        " ORDER BY score DESC, created_at DESC, id LIMIT :lim",
        p,
    )
    return [
        _item(
            "project",
            r["id"],
            _title(r["name"]),
            make_snippet(r["description"], q.raw),
            r["score"],
            r["created_at"],
            project_id=r["id"],
            api_path=f"/api/v1/projects/{r['id']}",
        )
        for r in rows
    ]


async def _runs(conn: AsyncConnection, q: _Query, p: dict[str, Any]) -> list[dict[str, Any]]:
    score = "1.0" if q.as_uuid else _score("objective")
    rows = await _rows(
        conn,
        f"SELECT id, project_id, mode, status, objective, created_at, {score} AS score FROM runs"  # noqa: S608 — подставляются только константы модуля
        f" WHERE workspace_id = :ws AND {_match(['objective'], q)}"
        " ORDER BY score DESC, created_at DESC, id LIMIT :lim",
        p,
    )
    return [
        _item(
            "run",
            r["id"],
            _title(r["objective"].split("\n", 1)[0]),
            make_snippet(r["objective"], q.raw),
            r["score"],
            r["created_at"],
            project_id=r["project_id"],
            run_id=r["id"],
            api_path=f"/api/v1/runs/{r['id']}",
        )
        for r in rows
    ]


async def _tasks(conn: AsyncConnection, q: _Query, p: dict[str, Any]) -> list[dict[str, Any]]:
    score = "1.0" if q.as_uuid else _score("t.title")
    match = "t.id = :uid" if q.as_uuid else _match(["t.title"], q)
    rows = await _rows(
        conn,
        f"SELECT t.id, t.run_id, r.project_id, t.title, t.task_key, t.agent_role, t.status, t.created_at,"  # noqa: S608 — подставляются только константы модуля
        f" {score} AS score FROM tasks t JOIN runs r ON r.workspace_id = t.workspace_id AND r.id = t.run_id"
        f" WHERE t.workspace_id = :ws AND {match}"
        " ORDER BY score DESC, t.created_at DESC, t.id LIMIT :lim",
        p,
    )
    return [
        _item(
            "task",
            r["id"],
            _title(r["title"]),
            f"{r['task_key']} · {r['agent_role']} · {r['status']}",
            r["score"],
            r["created_at"],
            project_id=r["project_id"],
            run_id=r["run_id"],
            task_id=r["id"],
            api_path=f"/api/v1/tasks/{r['id']}",
        )
        for r in rows
    ]


async def _findings(conn: AsyncConnection, q: _Query, p: dict[str, Any]) -> list[dict[str, Any]]:
    score = (
        "1.0"
        if q.as_uuid
        else f"GREATEST({_score('f.title')}, {_score('f.rule_id')}, CASE WHEN f.cwe_id ILIKE :contains"
        " ESCAPE '\\' THEN 0.7 ELSE 0 END)"
    )
    match = "f.id = :uid" if q.as_uuid else _match([_FINDING_TEXT], q)
    rows = await _rows(
        conn,
        "SELECT f.id, f.run_id, f.task_id, r.project_id, f.title, f.rule_id, f.severity, f.status, f.file_path,"  # noqa: S608 — подставляются только константы модуля
        f" f.line, f.created_at, {score} AS score"
        " FROM findings f JOIN runs r ON r.workspace_id = f.workspace_id AND r.id = f.run_id"
        f" WHERE f.workspace_id = :ws AND {match}"
        " ORDER BY score DESC, f.created_at DESC, f.id LIMIT :lim",
        p,
    )
    out = []
    for r in rows:
        location = f" · {r['file_path']}" + (f":{r['line']}" if r["line"] else "") if r["file_path"] else ""
        out.append(
            _item(
                "finding",
                r["id"],
                _title(r["title"]),
                redact_text(f"{r['severity']} · {r['status']} · {r['rule_id']}{location}")[:SNIPPET_CHARS],
                r["score"],
                r["created_at"],
                project_id=r["project_id"],
                run_id=r["run_id"],
                task_id=r["task_id"],
                api_path=f"/api/v1/findings/{r['id']}",
            )
        )
    return out


async def _approvals(conn: AsyncConnection, q: _Query, p: dict[str, Any]) -> list[dict[str, Any]]:
    score = (
        "1.0"
        if q.as_uuid
        else f"GREATEST({_score('action_type::text')}, CASE WHEN decision_reason ILIKE :contains"
        " ESCAPE '\\' THEN 0.5 ELSE 0 END)"
    )
    rows = await _rows(
        conn,
        "SELECT id, project_id, run_id, task_id, action_type::text AS action_type, status::text AS status,"  # noqa: S608 — подставляются только константы модуля
        f" risk_level::text AS risk_level, decision_reason, requested_at, {score} AS score FROM approvals"
        f" WHERE workspace_id = :ws AND {_match(['action_type::text', 'decision_reason'], q)}"
        " ORDER BY score DESC, requested_at DESC, id LIMIT :lim",
        p,
    )
    return [
        _item(
            "approval",
            r["id"],
            f"{r['action_type']} · {r['status']}",
            make_snippet(r["decision_reason"], q.raw) or f"risk {r['risk_level']}",
            r["score"],
            r["requested_at"],
            project_id=r["project_id"],
            run_id=r["run_id"],
            task_id=r["task_id"],
            api_path=f"/api/v1/approvals/{r['id']}",
        )
        for r in rows
    ]


async def _doc_sources(conn: AsyncConnection, q: _Query, p: dict[str, Any]) -> list[dict[str, Any]]:
    score = "1.0" if q.as_uuid else _score("uri_or_file_ref")
    rows = await _rows(
        conn,
        "SELECT id, source_type, uri_or_file_ref, trust_level, enabled, created_at,"  # noqa: S608 — подставляются только константы модуля
        f" {score} AS score FROM documentation_sources"
        f" WHERE workspace_id = :ws AND {_match(['uri_or_file_ref'], q)}"
        " ORDER BY score DESC, created_at DESC, id LIMIT :lim",
        p,
    )
    return [
        _item(
            "documentation_source",
            r["id"],
            _title(r["uri_or_file_ref"]),
            f"{r['source_type']} · {r['trust_level']}" + ("" if r["enabled"] else " · disabled"),
            r["score"],
            r["created_at"],
            api_path=f"/api/v1/documentation/sources?source_id={r['id']}",
        )
        for r in rows
    ]


async def _chat(conn: AsyncConnection, q: _Query, p: dict[str, Any]) -> list[dict[str, Any]]:
    if q.as_uuid is not None:
        sql = (
            "SELECT m.id, m.run_id, r.project_id, m.body, m.created_at, 1.0 AS score FROM chat_messages m"
            " JOIN runs r ON r.workspace_id = m.workspace_id AND r.id = m.run_id"
            " WHERE m.workspace_id = :ws AND m.id = :uid LIMIT :lim"
        )
    else:
        sql = (
            "SELECT m.id, m.run_id, r.project_id, m.body, m.created_at,"
            "       LEAST(0.9, 0.3 + ts_rank_cd(m.search_vector, tq.q)) AS score"
            "  FROM chat_messages m JOIN runs r ON r.workspace_id = m.workspace_id AND r.id = m.run_id,"
            "       (SELECT websearch_to_tsquery('simple', :q) AS q) tq"
            " WHERE m.workspace_id = :ws AND m.search_vector @@ tq.q"
            " ORDER BY score DESC, m.created_at DESC, m.id LIMIT :lim"
        )
    rows = await _rows(conn, sql, p)
    return [
        _item(
            "chat_message",
            r["id"],
            _title(r["body"].split("\n", 1)[0][:120]),
            make_snippet(r["body"], q.raw),
            r["score"],
            r["created_at"],
            project_id=r["project_id"],
            run_id=r["run_id"],
            api_path=f"/api/v1/runs/{r['run_id']}/chat",
        )
        for r in rows
    ]


_SEARCHERS: dict[str, Callable[[AsyncConnection, _Query, dict[str, Any]], Awaitable[list[dict[str, Any]]]]] = {
    "project": _projects,
    "run": _runs,
    "task": _tasks,
    "finding": _findings,
    "approval": _approvals,
    "documentation_source": _doc_sources,
    "chat_message": _chat,
}


def _parse_query(raw: str) -> _Query:
    q = raw.strip()
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in q):
        raise ProblemError(
            "validation_error", errors=[FieldError("/q", "invalid_query", "управляющие символы недопустимы")]
        )
    if len(q) < MIN_QUERY_CHARS:
        raise ProblemError(
            "validation_error",
            errors=[FieldError("/q", "too_short", f"запрос короче {MIN_QUERY_CHARS} символов")],
        )
    try:
        as_uuid: uuid.UUID | None = uuid.UUID(q) if len(q) == 36 else None
    except ValueError:
        as_uuid = None
    escaped = _like_escape(q)
    return _Query(raw=q, contains=f"%{escaped}%", prefix=f"{escaped}%", as_uuid=as_uuid)


async def _rate_limit(request: Request, principal: identity.Principal) -> None:
    state = app_state(request)
    decision = await state.limiter.hit(f"search:user:{principal.user_id}", state.settings.rate_limit_search_per_minute)
    request.state.rate = decision
    if not decision.allowed:
        raise ProblemError(
            "rate_limited",
            extensions={"retry_after_seconds": decision.reset_seconds},
            headers={"Retry-After": str(decision.reset_seconds)},
        )


@router.get("/search", **op("searchWorkspace", "project:read"))
async def search_workspace(
    request: Request,
    q: Annotated[str, Query(min_length=MIN_QUERY_CHARS, max_length=200)],
    principal: identity.Principal = Depends(current_principal),
    types: Annotated[list[SearchType] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> Response:
    await _rate_limit(request, principal)
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "project:read")
    query = _parse_query(q)
    requested = [t for t in TYPE_ORDER if types is None or t in types]
    allowed = [t for t in requested if has_permission(access.role, TYPE_PERMISSION[t])]
    params = {
        "ws": access.workspace_id,
        "q": query.raw,
        "contains": query.contains,
        "prefix": query.prefix,
        "uid": query.as_uuid,
        "lim": limit,
    }
    items: list[dict[str, Any]] = []
    async with transaction(app_state(request).engine, access.db()) as conn:
        await conn.execute(text("SET LOCAL statement_timeout = '5s'"))
        for kind in allowed:
            items.extend(await _SEARCHERS[kind](conn, query, params))
    rank = {t: i for i, t in enumerate(TYPE_ORDER)}
    items.sort(key=lambda it: (-it["score"], rank[it["type"]], -it["created_at"].timestamp(), str(it["id"])))
    return json_response(
        {
            "query": query.raw,
            "types": allowed,
            "skipped_types": [t for t in requested if t not in allowed],
            "items": items[:limit],
            "truncated": len(items) > limit,
        }
    )
