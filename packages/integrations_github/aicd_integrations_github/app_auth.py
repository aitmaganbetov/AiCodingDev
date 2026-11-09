"""JWT GitHub App (RS256) для обмена на installation access token (P8-02).

JWT живёт ≤ 10 минут (ограничение GitHub; берём 9 мин и `iat` на 60 с в прошлом против рассинхронизации
часов). Подпись — RSASSA-PKCS1-v1_5 / SHA-256 через `cryptography` (собственной реализации RSA нет).
Ключ App загружается из файла секрета; объект ключа не сериализуется и не логируется (`__repr__`).
"""

from __future__ import annotations

import base64
import json
import time
from typing import Final

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

JWT_TTL_SECONDS: Final = 540
CLOCK_SKEW_SECONDS: Final = 60


class AppKeyError(ValueError):
    """Ключ GitHub App отсутствует или не является RSA-ключом PEM."""


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class AppJwtSigner:
    def __init__(self, app_id: int, private_key_pem: str) -> None:
        if app_id <= 0:
            raise AppKeyError("github_app_id не задан")
        try:
            key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
        except (ValueError, TypeError) as exc:
            raise AppKeyError("ключ GitHub App не читается как PEM") from exc
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise AppKeyError("ключ GitHub App должен быть RSA ≥ 2048 бит")
        self._key = key
        self.app_id = app_id

    def __repr__(self) -> str:
        return f"AppJwtSigner(app_id={self.app_id}, key=***)"

    def issue(self, now: float | None = None) -> str:
        issued = int(now if now is not None else time.time()) - CLOCK_SKEW_SECONDS
        header = {"alg": "RS256", "typ": "JWT"}
        payload = {"iat": issued, "exp": issued + CLOCK_SKEW_SECONDS + JWT_TTL_SECONDS, "iss": str(self.app_id)}
        signing_input = (
            b64url(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + b64url(json.dumps(payload, separators=(",", ":")).encode())
        )
        signature = self._key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        return signing_input + "." + b64url(signature)
