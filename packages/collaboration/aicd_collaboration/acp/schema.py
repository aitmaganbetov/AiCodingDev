"""Шаг 1 конвейера ACP: JSON Schema (Draft 2020-12) конверта и payload по `type`.

Источник истины — `packages/contracts/schemas/acp/1.0` (ADR-0013); схемы здесь не дублируются,
а загружаются из контрактов и связываются через реестр `referencing` по их `$id`.
"""

from __future__ import annotations

import json
import os
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

import aicd_contracts
from aicd_collaboration.acp.model import MESSAGE_TYPES

ENV_SCHEMAS_DIR = "AICD_CONTRACTS_SCHEMAS_DIR"
ACP_VERSION_DIR = ("acp", "1.0")
MAX_ERRORS = 5


def schemas_root() -> Path:
    """Каталог `packages/contracts/schemas` (переопределяется `AICD_CONTRACTS_SCHEMAS_DIR`)."""
    override = os.environ.get(ENV_SCHEMAS_DIR)
    if override:
        return Path(override)
    return Path(aicd_contracts.__file__).resolve().parent.parent / "schemas"


def acp_dir() -> Path:
    return schemas_root().joinpath(*ACP_VERSION_DIR)


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


@lru_cache(maxsize=1)
def _schemas() -> tuple[dict[str, Any], dict[str, dict[str, Any]], Registry[Any]]:
    base = acp_dir()
    if not base.is_dir():
        raise FileNotFoundError(f"каталог схем ACP не найден: {base} (задайте {ENV_SCHEMAS_DIR})")
    envelope = _load(base / "envelope.schema.json")
    common = _load(base / "common.schema.json")
    payloads = {t: _load(base / "payloads" / f"{t}.schema.json") for t in MESSAGE_TYPES}
    resources = [envelope, common, *payloads.values()]
    registry: Registry[Any] = Registry().with_resources(
        (doc["$id"], DRAFT202012.create_resource(doc)) for doc in resources
    )
    return envelope, payloads, registry


@cache
def _validator(kind: str) -> Draft202012Validator:
    envelope, payloads, registry = _schemas()
    schema = envelope if kind == "envelope" else payloads[kind]
    resource: Resource[Any] = DRAFT202012.create_resource(schema)
    registry = registry.with_resource(schema["$id"], resource)
    return Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())


def _format(errors: list[Any], prefix: str) -> list[str]:
    out = []
    for err in sorted(errors, key=lambda e: (list(e.absolute_path), e.message))[:MAX_ERRORS]:
        path = "/".join(str(p) for p in err.absolute_path)
        # Сообщение jsonschema может цитировать недоверенное значение — обрезаем.
        out.append(f"{prefix}/{path}: {err.message[:200]}" if path else f"{prefix}: {err.message[:200]}")
    return out


def envelope_errors(envelope: Any) -> list[str]:
    """Ошибки схемы конверта и payload (пустой список — валидно)."""
    errors = _format(list(_validator("envelope").iter_errors(envelope)), "")
    if errors:
        return errors
    return payload_errors(envelope["type"], envelope["payload"])


def payload_errors(message_type: str, payload: Any) -> list[str]:
    if message_type not in MESSAGE_TYPES:
        return [f"/type: неизвестный тип {message_type[:50]!r}"]
    return _format(list(_validator(message_type).iter_errors(payload)), "/payload")


def warm_up() -> None:
    """Загрузить и скомпилировать все схемы (fail-fast при старте воркера/API)."""
    _validator("envelope")
    for t in MESSAGE_TYPES:
        _validator(t)
