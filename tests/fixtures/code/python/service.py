"""Сервис пользователей (фикстура golden-теста P5-03)."""

from __future__ import annotations

import os.path
from collections import OrderedDict as OD
from . import models
from ..core.base import BaseService, helper

MAX_USERS = 100
_cache = {}


@dataclass(frozen=True)
class UserService(BaseService):
    """Операции над пользователями.

    Второй абзац не попадает в выдержку.
    """

    DEFAULT_ROLE = "viewer"

    def __init__(self, repo: "Repo") -> None:
        self.repo = repo

    @property
    def size(self) -> int:
        return len(self.repo)

    async def fetch(self, user_id: int, *, include_deleted: bool = False) -> "User | None":
        """Загрузить пользователя."""

        def inner() -> None:
            pass

        return await self.repo.get(user_id)


def make_service(repo):
    try:
        import json  # noqa: F401
    except ImportError:
        pass
    return UserService(repo)
