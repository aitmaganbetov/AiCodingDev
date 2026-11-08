"""Проверка ответов API по `packages/contracts/openapi.yaml` (единый источник контракта)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).resolve().parents[2]
OPENAPI_PATH = ROOT / "packages" / "contracts" / "openapi.yaml"
DOC_URI = "urn:aicd:openapi"


@lru_cache(maxsize=1)
def openapi() -> dict[str, Any]:
    with OPENAPI_PATH.open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return data


@lru_cache(maxsize=1)
def _registry() -> Registry[Any]:
    resource: Resource[Any] = DRAFT202012.create_resource(openapi())
    registry: Registry[Any] = Registry().with_resource(DOC_URI, resource)
    return registry


def operations() -> dict[str, tuple[str, str, dict[str, Any]]]:
    """operationId → (METHOD, path, operation)."""
    result = {}
    for path, item in openapi()["paths"].items():
        for method, op in item.items():
            if method in ("get", "post", "put", "patch", "delete"):
                result[op["operationId"]] = (method.upper(), path, op)
    return result


def _response_schema(operation_id: str, status: int, media: str) -> dict[str, Any] | None:
    _, _, op = operations()[operation_id]
    responses = op["responses"]
    resp = responses.get(str(status)) or responses.get("default")
    if resp is None:
        raise AssertionError(f"{operation_id}: статус {status} не описан в контракте")
    if "$ref" in resp:
        resp = openapi()["components"]["responses"][resp["$ref"].rsplit("/", 1)[-1]]
    content = resp.get("content")
    if not content:
        return None
    if media not in content:
        raise AssertionError(f"{operation_id} {status}: media type {media} не описан ({list(content)})")
    schema: dict[str, Any] = content[media]["schema"]
    return schema


def _rewrite_refs(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            k: (DOC_URI + v if k == "$ref" and isinstance(v, str) and v.startswith("#") else _rewrite_refs(v))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_rewrite_refs(v) for v in node]
    return node


def validate_response(operation_id: str, response: Any) -> None:
    """Проверяет статус, тип содержимого и тело ответа httpx по контракту."""
    status = response.status_code
    media = response.headers.get("content-type", "").split(";")[0].strip()
    schema = _response_schema(operation_id, status, media or "application/json")
    if schema is None:
        assert not response.content, f"{operation_id} {status}: контракт не предусматривает тела"
        return
    validator = Draft202012Validator(_rewrite_refs(schema), registry=_registry(), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(response.json()), key=lambda e: list(e.path))
    assert not errors, f"{operation_id} {status}: " + "; ".join(
        f"{'/'.join(map(str, e.path))}: {e.message}" for e in errors[:5]
    )
