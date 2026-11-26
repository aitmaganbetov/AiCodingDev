"""Список репозиториев, доступных установке GitHub App («что разрешено»), — кэш в Redis.

Зачем кэш, а не таблица. Процесс API не держит закрытый ключ App (OQ-47 (2), SECURITY_ARCHITECTURE §6.1)
и не может спросить GitHub сам: состав установки читает воркер. При этом список — не состояние платформы,
а проекция внешнего состояния GitHub: он меняется вне платформы (владелец добавил/убрал репозиторий),
ничего не решает (доступ к конкретному репозиторию всё равно проверяется заново при привязке,
`github_verification`) и ничего не хранит о пользователе. Такому месту соответствует Redis
(SC-DATA-09: Redis — эфемерные подсказки, не источник истины), а не таблица с RLS и миграцией.

Поток: UI открывает форму привязки → `GET …/installations/{id}/repositories` читает кэш; пусто или устарел
→ `POST …/repositories/refresh` кладёт событие outbox → воркер `github` читает `/installation/repositories`
token'ом операции `list` (`metadata: read`) и пишет сюда же. Кэш переживает перезапуск API, теряется при
очистке Redis — тогда UI просто просит обновить.

Секретов в кэше нет: только публичные поля репозитория (владелец, имя, видимость, ветка по умолчанию).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from aicd_integrations_github.client import InstallationRepo, InstallationRepos

#: Сколько список считается свежим. Дольше — UI покажет его и предложит обновить (данные не выбрасываются:
#: показать вчерашний список лучше, чем пустой).
FRESH_SECONDS: Final = 15 * 60
#: Сколько запись живёт в Redis вообще.
TTL_SECONDS: Final = 24 * 60 * 60
#: Пауза между обновлениями одной установки: защита лимита GitHub API от частых нажатий «обновить».
REFRESH_COOLDOWN_SECONDS: Final = 20
#: Потолок числа записей в кэше (у клиента свой потолок страниц; здесь — размер значения в Redis).
MAX_ITEMS: Final = 2000


def key(environment: str, installation_id: uuid.UUID | str) -> str:
    return f"aicd:{environment}:github_repos:{installation_id}"


def _cooldown_key(environment: str, installation_id: uuid.UUID | str) -> str:
    return f"aicd:{environment}:github_repos_refresh:{installation_id}"


@dataclass(frozen=True)
class CachedRepositories:
    items: tuple[InstallationRepo, ...]
    #: Сколько репозиториев доступно установке по данным GitHub (может быть больше, чем в `items`).
    total: int
    #: Список обрезан (потолком страниц у клиента или `MAX_ITEMS` здесь).
    truncated: bool
    synced_at: datetime

    def fresh(self, now: datetime) -> bool:
        return (now - self.synced_at).total_seconds() < FRESH_SECONDS


def _dump(repo: InstallationRepo) -> dict[str, Any]:
    return {
        "id": repo.id,
        "owner": repo.owner,
        "name": repo.name,
        "private": repo.private,
        "archived": repo.archived,
        "default_branch": repo.default_branch,
        "html_url": repo.html_url,
    }


def _load(data: Any) -> InstallationRepo | None:
    if not isinstance(data, dict):
        return None
    try:
        return InstallationRepo(
            id=int(data["id"]),
            owner=str(data["owner"]),
            name=str(data["name"]),
            private=bool(data.get("private")),
            archived=bool(data.get("archived")),
            default_branch=str(data.get("default_branch") or ""),
            html_url=str(data["html_url"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


async def save(
    redis: Any, *, environment: str, installation_id: uuid.UUID | str, repos: InstallationRepos, now: datetime
) -> CachedRepositories:
    """Записать свежий список (воркер). Записи сверх `MAX_ITEMS` отбрасываются с пометкой `truncated`."""
    items = repos.items[:MAX_ITEMS]
    cached = CachedRepositories(
        items=items,
        total=max(repos.total, len(items)),
        truncated=repos.truncated or len(items) < len(repos.items),
        synced_at=now,
    )
    payload = json.dumps(
        {
            "synced_at": cached.synced_at.astimezone(UTC).isoformat(),
            "total": cached.total,
            "truncated": cached.truncated,
            "items": [_dump(r) for r in cached.items],
        },
        ensure_ascii=False,
    )
    await redis.set(key(environment, installation_id), payload, ex=TTL_SECONDS)
    return cached


async def load(redis: Any, *, environment: str, installation_id: uuid.UUID | str) -> CachedRepositories | None:
    """Прочитать список (API). None — кэша нет: UI предложит обновить."""
    raw = await redis.get(key(environment, installation_id))
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
        synced_at = datetime.fromisoformat(str(data["synced_at"]))
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(data.get("items"), list):
        return None
    items = tuple(r for r in (_load(entry) for entry in data["items"]) if r is not None)
    return CachedRepositories(
        items=items,
        total=int(data.get("total") or len(items)),
        truncated=bool(data.get("truncated")),
        synced_at=synced_at if synced_at.tzinfo else synced_at.replace(tzinfo=UTC),
    )


async def drop(redis: Any, *, environment: str, installation_id: uuid.UUID | str) -> None:
    """Забыть список (установка отключена или GitHub прислал `installation_repositories`)."""
    await redis.delete(key(environment, installation_id))


async def claim_refresh(redis: Any, *, environment: str, installation_id: uuid.UUID | str) -> bool:
    """True — обновление поставлено в очередь; False — та же установка обновлялась только что."""
    return bool(await redis.set(_cooldown_key(environment, installation_id), "1", nx=True, ex=REFRESH_COOLDOWN_SECONDS))
