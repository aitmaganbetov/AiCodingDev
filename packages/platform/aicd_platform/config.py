"""Конфигурация из переменных окружения `AICD_*`.

Секреты (пароли БД, ключ подписи курсоров, ключи хранилища) читаются из файлов
(`*_FILE`), чтобы не попадать в окружение процессов и `docker inspect` (SECURITY_ARCHITECTURE §6).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _read_secret_file(path: str | None) -> SecretStr | None:
    if not path:
        return None
    return SecretStr(Path(path).read_text(encoding="utf-8").strip())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AICD_", extra="ignore")

    environment: Literal["development", "test", "production"] = "production"

    # PostgreSQL: подключение под ролью aicd_app (NOBYPASSRLS).
    database_url: SecretStr | None = None
    database_url_file: str | None = None
    db_pool_size: int = Field(default=10, ge=1, le=200)

    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_url_file: str | None = None  # URL с паролем — из файла секрета

    # Ключ HMAC для cursor-пагинации и прочих подписей инстанса.
    secret_key: SecretStr | None = None
    secret_key_file: str | None = None

    # Сессии (API_DESIGN §3): абсолютный TTL 12 ч, idle 30 мин.
    session_absolute_ttl_seconds: int = Field(default=12 * 3600, ge=300)
    session_idle_ttl_seconds: int = Field(default=30 * 60, ge=60)
    session_cookie_name: str = "aicd_session"
    csrf_cookie_name: str = "aicd_csrf"
    cookie_secure: bool = True
    # Разрешённые Origin для небезопасных методов (защита от CSRF, defence in depth).
    allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Блокировка входа (API_DESIGN §3, §10): 5 неудач ⇒ 15 минут.
    login_max_failures: int = Field(default=5, ge=1)
    login_lockout_seconds: int = Field(default=15 * 60, ge=60)

    # Rate limits (API_DESIGN §10), запросов в минуту.
    rate_limit_read_per_minute: int = 600
    rate_limit_mutation_per_minute: int = 120
    rate_limit_login_per_ip_per_minute: int = 10
    rate_limit_anonymous_per_ip_per_minute: int = 60

    # Allowlist корней локальных репозиториев: "имя=/абсолютный/путь" (FR-PRJ-02).
    repository_roots: Annotated[dict[str, Path], NoDecode] = Field(default_factory=dict)

    allow_workspace_creation: bool = True

    # Хранилище артефактов (P1-15, OQ-11: FS по умолчанию).
    artifact_backend: Literal["fs", "s3"] = "fs"
    artifact_fs_root: Path = Path("/var/lib/aicd/artifacts")
    artifact_s3_endpoint: str | None = None
    artifact_s3_bucket: str = "aicd-artifacts"
    artifact_s3_region: str = "us-east-1"
    artifact_s3_access_key_file: str | None = None
    artifact_s3_secret_key_file: str | None = None
    artifact_download_ttl_seconds: int = Field(default=300, ge=10, le=3600)

    log_level: str = "INFO"
    otel_exporter: Literal["none", "console"] = "none"

    @field_validator("repository_roots", mode="before")
    @classmethod
    def _parse_roots(cls, value: object) -> object:
        if isinstance(value, str):
            roots: dict[str, Path] = {}
            for item in filter(None, (p.strip() for p in value.split(","))):
                name, _, path = item.partition("=")
                roots[name.strip()] = Path(path.strip())
            return roots
        return value

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _parse_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [o.strip() for o in value.split(",") if o.strip()]
        return value

    @model_validator(mode="after")
    def _load_secret_files(self) -> Settings:
        if self.database_url is None:
            self.database_url = _read_secret_file(self.database_url_file)
        if self.secret_key is None:
            self.secret_key = _read_secret_file(self.secret_key_file)
        redis_from_file = _read_secret_file(self.redis_url_file)
        if redis_from_file is not None:
            self.redis_url = redis_from_file.get_secret_value()
        for name, root in self.repository_roots.items():
            if not name.replace("-", "").replace("_", "").isalnum():
                raise ValueError(f"недопустимое имя корня репозиториев: {name!r}")
            if not root.is_absolute():
                raise ValueError(f"корень репозиториев {name!r} должен быть абсолютным путём")
        return self

    def require_database_url(self) -> str:
        if self.database_url is None:
            raise RuntimeError("AICD_DATABASE_URL или AICD_DATABASE_URL_FILE не заданы")
        return self.database_url.get_secret_value()

    def require_secret_key(self) -> bytes:
        if self.secret_key is None:
            raise RuntimeError("AICD_SECRET_KEY или AICD_SECRET_KEY_FILE не заданы")
        key = self.secret_key.get_secret_value().encode()
        if len(key) < 32:
            raise RuntimeError("AICD_SECRET_KEY должен содержать не менее 32 байт")
        return key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
