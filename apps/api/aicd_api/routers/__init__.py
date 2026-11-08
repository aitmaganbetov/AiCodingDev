"""Маршруты API v1. Каждая операция объявляет operationId и x-required-permission из контракта."""

from typing import Any


def op(operation_id: str, permission: str) -> dict[str, Any]:
    return {"operation_id": operation_id, "openapi_extra": {"x-required-permission": permission}}
