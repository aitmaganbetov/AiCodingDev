"""Обработчики исключений → RFC 7807 problem+json (API_DESIGN §6)."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from aicd_api.responses import problem_response
from aicd_platform.errors import FieldError, ProblemError

log = logging.getLogger("aicd.api")


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _pointer(loc: tuple[object, ...]) -> str:
    parts = [str(p) for p in loc]
    if parts and parts[0] in ("body",):
        parts = parts[1:]
    elif parts and parts[0] in ("query", "header", "path", "cookie"):
        return f"{parts[0]}:" + ".".join(parts[1:])
    return "/" + "/".join(p.replace("~", "~0").replace("/", "~1") for p in parts)


def install(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem(request: Request, exc: ProblemError) -> Response:
        return problem_response(exc, instance=request.url.path, request_id=_request_id(request))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> Response:
        errors = []
        malformed = False
        for err in exc.errors():
            if err.get("type") == "json_invalid":
                malformed = True
            # Значения ввода не возвращаются (могут содержать секреты, например пароль).
            errors.append(FieldError(_pointer(tuple(err.get("loc", ()))), str(err.get("type")), str(err.get("msg"))))
        problem = ProblemError("malformed_request") if malformed else ProblemError("validation_error", errors=errors)
        return problem_response(problem, instance=request.url.path, request_id=_request_id(request))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> Response:
        code = {404: "not_found", 405: "malformed_request", 413: "payload_too_large"}.get(
            exc.status_code, "malformed_request"
        )
        return problem_response(ProblemError(code), instance=request.url.path, request_id=_request_id(request))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        log.exception("unhandled error")
        return problem_response(
            ProblemError("internal_error"), instance=request.url.path, request_id=_request_id(request)
        )
