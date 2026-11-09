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
    # TCP keepalive / tcp_user_timeout соединений libpq (P9-03): «чёрная дыра» сети (пакеты теряются без RST)
    # обнаруживается за idle + interval × count секунд вместо ~2 ч по умолчанию ядра; 0 в tcp_user_timeout — выкл.
    db_keepalives_idle_seconds: int = Field(default=30, ge=1, le=7200)
    db_keepalives_interval_seconds: int = Field(default=10, ge=1, le=600)
    db_keepalives_count: int = Field(default=3, ge=1, le=60)
    db_tcp_user_timeout_ms: int = Field(default=60_000, ge=0, le=3_600_000)

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
    # Глобальный поиск `GET /search` (B6): отдельный бакет на пользователя поверх общего лимита чтения.
    rate_limit_search_per_minute: int = Field(default=60, ge=1)

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

    # Обслуживание БД (DATABASE_DESIGN §19.6, §20): подключение роли aicd_maintenance — только из файла
    # секрета/окружения команды `aicd maintenance …`, процессы api/worker его не получают.
    maintenance_database_url: SecretStr | None = None
    maintenance_database_url_file: str | None = None
    # Сроки хранения по умолчанию (§20.3); глобальная политика policies(policy_type='retention') их переопределяет.
    retention_run_event_log_days: int = Field(default=30, ge=1)
    retention_agent_activity_days: int = Field(default=90, ge=1)
    retention_agent_messages_days: int = Field(default=365, ge=1)
    retention_audit_days: int = Field(default=400, ge=30)
    retention_outbox_published_months: int = Field(default=2, ge=1, le=24)
    retention_processed_events_days: int = Field(default=30, ge=1)
    retention_user_sessions_days: int = Field(default=30, ge=1)
    retention_provider_health_days: int = Field(default=30, ge=1)
    retention_webhook_deliveries_days: int = Field(default=30, ge=1)
    retention_usage_metrics_days: int = Field(default=365, ge=1)
    retention_index_snapshots_failed_days: int = Field(default=7, ge=1)
    retention_index_snapshots_keep_ready: int = Field(default=5, ge=1)
    retention_documentation_superseded_days: int = Field(default=90, ge=1)
    retention_artifacts_ephemeral_days: int = Field(default=7, ge=1)
    retention_artifacts_raw_log_days: int = Field(default=30, ge=1)
    retention_artifacts_standard_days: int = Field(default=180, ge=1)
    retention_notifications_days: int = Field(default=90, ge=1)  # уведомления пользователей (миграция 0020)
    # Партиции создаются на N месяцев вперёд; алерт, если впереди меньше 2 (§20.1).
    retention_partitions_ahead_months: int = Field(default=3, ge=2, le=24)
    # Пакетное удаление непартиционированных таблиц (§20.2): 5–10 тыс. строк с паузами.
    retention_batch_size: int = Field(default=5000, ge=1, le=10_000)
    retention_batch_pause_seconds: float = Field(default=0.05, ge=0, le=10)
    # Приёмник архива NDJSON.gz (§20.2): fs | s3; не задан — как artifact_backend.
    retention_archive_backend: Literal["fs", "s3"] | None = None
    # Каталог архива (fs); по умолчанию <artifact_fs_root>/archive.
    retention_archive_root: Path | None = None
    # Bucket архива (s3; по умолчанию artifact_s3_bucket — рекомендуется отдельный bucket с object lock/WORM)
    # и префикс ключей: `<prefix>/<workspace_id|global>/<table|runs>/<period|run_id>.ndjson.gz`.
    # Endpoint, регион и ключи доступа — общие с хранилищем артефактов (AICD_ARTIFACT_S3_*).
    retention_archive_s3_bucket: str | None = None
    retention_archive_s3_prefix: str = Field(default="archive", pattern=r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")

    log_level: str = "INFO"
    otel_exporter: Literal["none", "console"] = "none"

    # GitHub App (P8-02/P8-03, SECURITY_ARCHITECTURE §6.1, §10): ключ App и webhook secret(ы) — только файлы
    # секретов (Docker secrets); при ротации активны два webhook secret (текущий и предыдущий, SC-SEC-06).
    # Базовые URL — конфигурация оператора (в production — только https); исходящие запросы к GitHub идут
    # через egress-proxy (`egress_proxy_url`), если он задан.
    github_app_id: int | None = Field(default=None, ge=1)
    github_app_slug: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    github_app_private_key_file: str | None = None
    github_webhook_secret_file: str | None = None
    github_webhook_secret_previous_file: str | None = None
    github_api_url: str = "https://api.github.com"
    github_web_url: str = "https://github.com"
    github_git_url: str | None = None  # транспорт git push (по умолчанию github_web_url)
    github_webhook_max_bytes: int = Field(default=25 * 1024 * 1024, ge=1024, le=25 * 1024 * 1024)
    github_install_state_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    egress_proxy_url: str | None = None

    # Метрики Prometheus (P8-06, SC-OBS-03): `/metrics` только во внутренней сети и по bearer-токену из файла
    # секрета; в production без токена endpoint выключен. Воркеры поднимают свой HTTP-сервер на
    # `metrics_port` (0 — не поднимать), API отдаёт `/metrics` вне `/api/v1` (reverse-proxy его не публикует).
    metrics_token: SecretStr | None = None
    metrics_token_file: str | None = None
    metrics_host: str = "0.0.0.0"  # noqa: S104 — порт доступен только в сети backend (compose), не публикуется
    metrics_port: int = Field(default=0, ge=0, le=65535)

    # Файл статуса утилиты бэкапа (`python -m scripts.backup … --status-file`, P9-05), смонтированный только для
    # чтения: worker-outbox — метрики aicd_backup_* и алерты AicdBackup* (ТЗ §16 «failed backups»); API — компонент
    # `backup` в `GET /system/health` (если файл смонтирован и в контейнер api; иначе статус `unknown`).
    backup_status_file: Path | None = None

    # Heartbeat процессов воркеров (`worker_hosts.last_heartbeat_at`): период записи; интервал пишется и в
    # `worker_hosts.labels.heartbeat_interval_seconds`, по нему `GET /system/health` и метрика
    # `aicd_worker_heartbeat_age_seconds` судят о живости (ok ≤ 3×, degraded ≤ 10×, иначе down).
    worker_heartbeat_seconds: float = Field(default=15.0, ge=1.0, le=300.0)
    # Виды воркеров, которые должны работать в этой установке (compose: outbox,orchestrator,exec,index). Пусто —
    # ожидания не заданы: отсутствие вида в `GET /system/health` — `unknown`, а не `down`.
    expected_worker_kinds: Annotated[
        list[Literal["api", "orchestrator", "outbox", "exec", "verify", "index"]], NoDecode
    ] = Field(default_factory=list)

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

    @field_validator("allowed_origins", "expected_worker_kinds", mode="before")
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
        if self.maintenance_database_url is None:
            self.maintenance_database_url = _read_secret_file(self.maintenance_database_url_file)
        if self.metrics_token is None:
            self.metrics_token = _read_secret_file(self.metrics_token_file)
        redis_from_file = _read_secret_file(self.redis_url_file)
        if redis_from_file is not None:
            self.redis_url = redis_from_file.get_secret_value()
        for name, root in self.repository_roots.items():
            if not name.replace("-", "").replace("_", "").isalnum():
                raise ValueError(f"недопустимое имя корня репозиториев: {name!r}")
            if not root.is_absolute():
                raise ValueError(f"корень репозиториев {name!r} должен быть абсолютным путём")
        if self.environment == "production":
            for name in ("github_api_url", "github_web_url", "github_git_url"):
                value = getattr(self, name)
                if value is not None and not str(value).startswith("https://"):
                    raise ValueError(f"{name} в production должен использовать https://")
        return self

    def require_database_url(self) -> str:
        if self.database_url is None:
            raise RuntimeError("AICD_DATABASE_URL или AICD_DATABASE_URL_FILE не заданы")
        return self.database_url.get_secret_value()

    def require_maintenance_database_url(self) -> str:
        if self.maintenance_database_url is None:
            raise RuntimeError("AICD_MAINTENANCE_DATABASE_URL или AICD_MAINTENANCE_DATABASE_URL_FILE не заданы")
        return self.maintenance_database_url.get_secret_value()

    def metrics_token_value(self) -> str | None:
        return self.metrics_token.get_secret_value() if self.metrics_token else None

    def github_private_key_pem(self) -> SecretStr | None:
        """PEM-ключ GitHub App из файла секрета (читается при обращении, в атрибутах Settings не хранится)."""
        return _read_secret_file(self.github_app_private_key_file)

    def github_webhook_secrets(self) -> list[SecretStr]:
        """Активные webhook secret: текущий и (на время ротации) предыдущий."""
        found = (
            _read_secret_file(self.github_webhook_secret_file),
            _read_secret_file(self.github_webhook_secret_previous_file),
        )
        return [s for s in found if s is not None and s.get_secret_value()]

    def archive_root(self) -> Path:
        return self.retention_archive_root or self.artifact_fs_root / "archive"

    def archive_backend(self) -> Literal["fs", "s3"]:
        return self.retention_archive_backend or self.artifact_backend

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
