"""Параметр `state` потока установки GitHub App (P8-02, защита от CSRF/подмены установки).

`state` = `v1.<payload b64url>.<HMAC-SHA256 b64url>`; payload — workspace, пользователь, nonce, срок.
Проверка: подпись (constant-time), срок, совпадение workspace и пользователя сессии. Однократность nonce
обеспечивает вызывающий (Redis `SET NX`). Подделать `state` для чужого workspace/сессии нельзя без ключа
инстанса (`secret_key`), а перехваченный `state` бесполезен вне сессии того же пользователя (CSRF-cookie +
заголовок X-CSRF-Token проверяются для POST подключения отдельно).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass

_DOMAIN = b"aicd.github.install_state.v1|"


class InstallStateError(ValueError):
    pass


@dataclass(frozen=True)
class InstallState:
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    nonce: str
    expires_at: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: bytes, payload: str) -> str:
    return _b64(hmac.new(key, _DOMAIN + payload.encode("ascii"), hashlib.sha256).digest())


def issue(key: bytes, workspace_id: uuid.UUID, user_id: uuid.UUID, ttl_seconds: int) -> tuple[str, InstallState]:
    state = InstallState(workspace_id, user_id, secrets.token_urlsafe(18), int(time.time()) + ttl_seconds)
    payload = _b64(
        json.dumps(
            {"w": str(workspace_id), "u": str(user_id), "n": state.nonce, "e": state.expires_at},
            separators=(",", ":"),
        ).encode()
    )
    return f"v1.{payload}.{_mac(key, payload)}", state


def verify(key: bytes, token: str, *, workspace_id: uuid.UUID, user_id: uuid.UUID) -> InstallState:
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "v1" or len(token) > 1024:
        raise InstallStateError("malformed")
    payload, mac = parts[1], parts[2]
    if not hmac.compare_digest(_mac(key, payload), mac):
        raise InstallStateError("signature")
    try:
        data = json.loads(_unb64(payload))
        state = InstallState(uuid.UUID(data["w"]), uuid.UUID(data["u"]), str(data["n"]), int(data["e"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise InstallStateError("malformed") from exc
    if state.expires_at < int(time.time()):
        raise InstallStateError("expired")
    if state.workspace_id != workspace_id or state.user_id != user_id:
        raise InstallStateError("mismatch")
    return state
