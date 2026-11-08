"""Загрузка нормативных JSON Schema из `packages/contracts/schemas` (AgentResult 1.0, ACP 1.0).

Схемы не дублируются в коде: валидаторы строятся из файлов контрактов. В editable-установке каталог
схем лежит рядом с пакетом `aicd_contracts`; в wheel — внутри него (`aicd_contracts/schemas`,
см. `force-include` в pyproject). Ссылки `../common.schema.json#…` разрешаются через registry по `$id`.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any, Final

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

import aicd_contracts

AGENT_RESULT_SCHEMA: Final = "agent/1.0/agent-result.schema.json"
ACP_ENVELOPE_SCHEMA: Final = "acp/1.0/envelope.schema.json"


@cache
def schemas_root() -> Path:
    pkg = Path(aicd_contracts.__file__).resolve().parent
    for candidate in (pkg / "schemas", pkg.parent / "schemas"):
        if (candidate / AGENT_RESULT_SCHEMA).is_file():
            return candidate
    raise FileNotFoundError("каталог схем контрактов не найден (packages/contracts/schemas)")


@cache
def load_schema(relative: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((schemas_root() / relative).read_text(encoding="utf-8"))
    return data


@cache
def _registry() -> Registry[Any]:
    resources = []
    for path in sorted(schemas_root().rglob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        if "$id" in schema:
            resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


@cache
def validator(relative: str) -> Draft202012Validator:
    return Draft202012Validator(load_schema(relative), registry=_registry(), format_checker=FormatChecker())


def acp_payload_validator(message_type: str) -> Draft202012Validator:
    return validator(f"acp/1.0/payloads/{message_type}.schema.json")
