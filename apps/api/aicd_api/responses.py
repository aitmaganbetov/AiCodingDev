"""Ответы JSON и problem+json."""

from __future__ import annotations

from typing import Any

from starlette.responses import Response

from aicd_api.serialization import dumps
from aicd_platform.errors import ProblemError

PROBLEM_MEDIA_TYPE = "application/problem+json"


def json_response(body: Any, status_code: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(dumps(body), status_code=status_code, media_type="application/json", headers=headers)


def problem_body(err: ProblemError, *, instance: str, request_id: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": f"urn:aicd:problem:{err.code}",
        "title": err.title,
        "status": err.status,
        "code": err.code,
        "instance": instance,
    }
    if err.detail:
        body["detail"] = err.detail
    if request_id:
        body["request_id"] = request_id
    if err.errors:
        body["errors"] = [{"pointer": e.pointer, "code": e.code, "message": e.message} for e in err.errors]
    body.update(err.extensions)
    return body


def problem_response(err: ProblemError, *, instance: str, request_id: str | None) -> Response:
    return Response(
        dumps(problem_body(err, instance=instance, request_id=request_id)),
        status_code=err.status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=err.headers or None,
    )


def etag(version: int) -> str:
    return f'"v{version}"'


def parse_if_match(value: str | None) -> int | None:
    """Разбирает сильный ETag `"v<n>"`. Слабые ETag (W/) для If-Match не принимаются (RFC 9110)."""
    if value is None:
        return None
    value = value.strip()
    if not (value.startswith('"v') and value.endswith('"')):
        raise ProblemError("precondition_failed")
    try:
        return int(value[2:-1])
    except ValueError as exc:
        raise ProblemError("precondition_failed") from exc


def require_if_match(value: str | None) -> int:
    version = parse_if_match(value)
    if version is None:
        raise ProblemError("precondition_required")
    return version
