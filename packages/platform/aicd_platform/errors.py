"""Ошибки предметной области, отображаемые в RFC 7807 problem+json (API_DESIGN §6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROBLEM_TITLES: dict[str, tuple[int, str]] = {
    "malformed_request": (400, "Некорректный запрос"),
    "idempotency_key_missing": (400, "Не передан Idempotency-Key"),
    "unauthenticated": (401, "Требуется аутентификация"),
    "session_expired": (401, "Сессия истекла"),
    "csrf_failed": (403, "Проверка CSRF не пройдена"),
    "forbidden": (403, "Недостаточно прав"),
    "four_eyes_violation": (403, "Требуется решение другого пользователя"),
    "policy_denied": (403, "Запрещено политикой"),
    "not_found": (404, "Ресурс не найден"),
    "conflict": (409, "Конфликт"),
    "version_mismatch": (409, "Версия не совпадает"),
    "invalid_state_transition": (409, "Недопустимый переход состояния"),
    "idempotency_key_in_flight": (409, "Запрос с этим Idempotency-Key ещё выполняется"),
    "last_owner": (409, "Нельзя удалить или понизить последнего владельца"),
    "replay_window_exceeded": (410, "Окно повторной доставки событий истекло"),
    "precondition_failed": (412, "Версия ресурса изменилась"),
    "payload_too_large": (413, "Слишком большое тело запроса"),
    "unsupported_media_type": (415, "Неподдерживаемый тип содержимого"),
    "validation_error": (422, "Ошибка валидации"),
    "invalid_cursor": (422, "Некорректный курсор"),
    "idempotency_key_reuse": (422, "Idempotency-Key использован с другим запросом"),
    "precondition_required": (428, "Требуется заголовок If-Match"),
    "rate_limited": (429, "Слишком много запросов"),
    "internal_error": (500, "Внутренняя ошибка"),
    "provider_unavailable": (503, "Провайдер недоступен"),
    "service_unavailable": (503, "Сервис недоступен"),
}


@dataclass(frozen=True)
class FieldError:
    pointer: str
    code: str
    message: str


@dataclass
class ProblemError(Exception):
    """Исключение, которое API превращает в problem+json.

    `detail` не должен содержать stack trace, SQL, пути хоста, секреты или сведения
    о существовании чужих ресурсов (API_DESIGN §6).
    """

    code: str
    detail: str | None = None
    errors: list[FieldError] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in PROBLEM_TITLES:
            raise ValueError(f"неизвестный код проблемы: {self.code}")
        super().__init__(self.code)

    @property
    def status(self) -> int:
        return PROBLEM_TITLES[self.code][0]

    @property
    def title(self) -> str:
        return PROBLEM_TITLES[self.code][1]


class VersionConflictError(ProblemError):
    def __init__(self, current_version: int | None = None) -> None:
        ext = {} if current_version is None else {"current_version": current_version}
        super().__init__("precondition_failed", extensions=ext)
