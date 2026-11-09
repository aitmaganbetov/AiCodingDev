"""Приём вебхуков GitHub (P8-03, SC-GH-01…03, SECURITY_ARCHITECTURE §10).

* Подпись: `X-Hub-Signature-256: sha256=<hex>` = HMAC-SHA256(secret, **сырое тело**); проверяется до разбора
  JSON, сравнение `hmac.compare_digest` с каждым активным секретом (два — на время ротации), без раннего
  выхода по первому совпадению.
* Дедупликация: `X-GitHub-Delivery` (UUID) → `github_webhook_deliveries.delivery_guid UNIQUE` (БД).
* Replay: GitHub не подписывает время отправки, поэтому окна по времени нет — защита = дедупликация GUID +
  payload как **подсказка**: обработчик перечитывает состояние через GitHub API (SC-GH-03).
* Allowlist событий; прочие подписанные события фиксируются `IGNORED` без обработки.
* Из payload извлекается только ограниченная подсказка (`Hint`) — она уходит в outbox; сам payload не хранится.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

SIGNATURE_PREFIX: Final = "sha256="
ALLOWED_EVENTS: Final = frozenset(
    {
        "ping",
        "installation",
        "installation_repositories",
        "pull_request",
        "pull_request_review",
        "check_run",
        "check_suite",
        "status",
        "push",
    }
)
_EVENT = re.compile(r"^[a-z_]{1,64}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class WebhookRejectedError(ValueError):
    """Запрос не является корректной доставкой GitHub (заголовки/тело). `code` — для аудита."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def signature_valid(body: bytes, header: str | None, secrets: Sequence[bytes]) -> bool:
    """Constant-time проверка подписи по сырому телу всеми активными секретами."""
    if not header or not header.startswith(SIGNATURE_PREFIX) or not secrets:
        return False
    received = header[len(SIGNATURE_PREFIX) :].strip().lower()
    if not _HEX64.fullmatch(received):
        return False
    matched = False
    for secret in secrets:
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        matched |= hmac.compare_digest(expected, received)
    return matched


def sign(body: bytes, secret: bytes) -> str:
    """Заголовок подписи (для тестов и инструментов оператора)."""
    return SIGNATURE_PREFIX + hmac.new(secret, body, hashlib.sha256).hexdigest()


def parse_delivery_guid(value: str | None) -> uuid.UUID:
    if not value:
        raise WebhookRejectedError("missing_delivery")
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise WebhookRejectedError("invalid_delivery") from exc


def parse_event(value: str | None) -> str:
    if not value or not _EVENT.fullmatch(value):
        raise WebhookRejectedError("invalid_event")
    return value


@dataclass(frozen=True)
class Hint:
    """Ограниченная выжимка payload (подсказка для обработчика; решения — по состоянию из API)."""

    action: str | None
    installation_id: int | None
    data: dict[str, Any] = field(default_factory=dict)


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 2**62:
        return None
    return value


def _str(value: Any, limit: int = 255) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def extract_hint(event: str, body: bytes) -> Hint:
    """Разбор JSON уже подписанного тела. Некорректный JSON — `WebhookRejectedError('malformed_payload')`."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebhookRejectedError("malformed_payload") from exc
    if not isinstance(payload, Mapping):
        raise WebhookRejectedError("malformed_payload")
    action = _str(payload.get("action"), 64)
    if action is not None and not _EVENT.fullmatch(action):
        action = None
    installation = payload.get("installation")
    installation_id = _int(installation.get("id")) if isinstance(installation, Mapping) else None
    data: dict[str, Any] = {}
    repo = payload.get("repository")
    if isinstance(repo, Mapping):
        data["repository_id"] = _int(repo.get("id"))
        data["repository_full_name"] = _str(repo.get("full_name"))
    pr = payload.get("pull_request")
    if isinstance(pr, Mapping):
        data["pr_number"] = _int(pr.get("number"))
        head = pr.get("head")
        if isinstance(head, Mapping):
            data["head_ref"] = _str(head.get("ref"))
            data["head_sha"] = _str(head.get("sha"), 64)
    elif event in ("pull_request_review",):
        data["pr_number"] = None
    if event == "installation" and isinstance(installation, Mapping):
        account = installation.get("account")
        if isinstance(account, Mapping):
            data["account_login"] = _str(account.get("login"), 39)
    if event == "status":
        data["sha"] = _str(payload.get("sha"), 64)
        data["state"] = _str(payload.get("state"), 32)
        data["context"] = _str(payload.get("context"))
    return Hint(action=action, installation_id=installation_id, data={k: v for k, v in data.items() if v is not None})


def payload_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
