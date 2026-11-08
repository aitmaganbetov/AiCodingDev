"""Образы verification-контейнеров по инструментам (QUALITY_GATES §4.1, ADR-0010).

Каждый gate исполняется в образе своего toolchain; ключ образа — `GateSpec.image_key` (по умолчанию
имя инструмента). Все образы закреплены по digest: тег в ссылке оставлен только для читаемости,
Docker разрешает образ по `@sha256:`. В профиле `server` (production) линтер контейнеров требует digest
(`DockerSandboxRunner(require_image_digest=True)`), а `validate_image_map` дополнительно отвергает
`latest` и ссылки без digest ещё при сборке конфигурации воркера.

Подключение (apps/worker): `VerificationEngine(images=default_gate_images(), image=DEFAULT_GATE_IMAGES["python"])`
— см. отчёт P6-03; переопределение — `AICD_VERIFICATION_IMAGES` (JSON {ключ: ссылка}) поверх этой таблицы.

Образы `pipelinecomponents/*` — сторонние сборки инструментов (Docker Hub); для production платформа
собирает собственные образы `aicd/verify-*` (P9) и заменяет ссылки здесь, сохраняя ключи.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")

# Ключ → ссылка на образ (pinned by digest). Значения проверены `docker pull` 2026-11-08.
DEFAULT_GATE_IMAGES: Mapping[str, str] = {
    # python3 + stdlib: SCOPE-сканер, pytest/mypy при наличии в образе (prepare-шаг P6: зависимости проекта).
    "python": "python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df",
    # ruff 0.16.x
    "ruff": "pipelinecomponents/ruff@sha256:7aebc033217963bac329075c40c99b49a7e6b1f6f57527173a09118d8c6c2fc4",
    # eslint 10.x (NODE_PATH=/app/node_modules)
    "eslint": "pipelinecomponents/eslint@sha256:7dbb5afec3be359fa1024a4cc76efc243fb6ce1333a9c5ab216b2d5a9039203a",
    # node 22 + npm: tsc/vitest/playwright/npm build из node_modules репозитория (только в контейнере).
    "node": "node:22-alpine@sha256:0e1754758c2df4cfa3e68d59e2e1b7374bb7fd9ea6328eaaacf15cc934af8202",
    # Эфемерная PostgreSQL для MIGRATION (P6-05): initdb/pg_ctl/psql, сеть none, данные в tmpfs.
    "postgres": "postgres:17-alpine@sha256:3540c0362c809df084ffac0ae69fe794515f9de92692f02178bcdce0ef0302ac",
}

# Инструмент gate → ключ образа (если не задан явно в GateSpec.image_key).
TOOL_IMAGE_KEYS: Mapping[str, str] = {
    "aicd-scope": "python",
    "ruff": "ruff",
    "pytest": "python",
    "coverage": "python",
    "mypy": "python",
    "pyright": "node",
    "python-build": "python",
    "eslint": "eslint",
    "tsc": "node",
    "npm": "node",
    "vitest": "node",
    "playwright": "node",
    "phpstan": "php",  # образа по умолчанию нет: gate даёт ERROR(TOOL_UNAVAILABLE), пока платформа его не задаст
    "postgres": "postgres",
    "alembic": "migrations-python",  # postgres + python + alembic: собирается платформой (не задан по умолчанию)
}


class ImagePolicyError(ValueError):
    pass


def is_pinned(ref: str) -> bool:
    return bool(DIGEST_RE.search(ref))


def validate_image_map(images: Mapping[str, str], *, require_digest: bool) -> list[str]:
    """Нарушения политики образов: `latest`, отсутствие digest (для server), пустые ссылки."""
    errors: list[str] = []
    for key, ref in sorted(images.items()):
        if not ref or any(c.isspace() for c in ref):
            errors.append(f"{key}: empty or malformed image reference")
            continue
        name = ref.split("@", 1)[0]
        tag = name.rsplit(":", 1)[1] if ":" in name.rsplit("/", 1)[-1] else None
        if tag == "latest" or (tag is None and not is_pinned(ref)):
            errors.append(f"{key}: image {ref!r} uses an implicit/latest tag")
        if require_digest and not is_pinned(ref):
            errors.append(f"{key}: image {ref!r} is not pinned by digest")
    return errors


def default_gate_images(overrides: Mapping[str, str] | None = None, *, require_digest: bool = True) -> dict[str, str]:
    """Таблица образов с переопределениями; при нарушении политики — `ImagePolicyError`."""
    merged = {**DEFAULT_GATE_IMAGES, **(overrides or {})}
    errors = validate_image_map(merged, require_digest=require_digest)
    if errors:
        raise ImagePolicyError("; ".join(errors))
    return merged


def image_key_for(tool: str, explicit: str | None = None) -> str:
    return explicit or TOOL_IMAGE_KEYS.get(tool, tool)
