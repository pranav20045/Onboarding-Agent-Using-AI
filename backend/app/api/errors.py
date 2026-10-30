"""Consistent JSON errors: {"error": {"code": ..., "message": ..., "details"?: ...}}.

Unexpected exceptions are logged server-side and returned as a generic 500 without internals.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, details: object | None = None,
                 headers: dict[str, str] | None = None) -> None:  # fmt: skip
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers


def error_body(code: str, message: str, details: object | None = None) -> dict:
    err: dict = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    return {"error": err}


_STATUS_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
                 405: "method_not_allowed", 409: "conflict", 413: "payload_too_large",
                 415: "unsupported_media_type", 429: "rate_limited"}  # fmt: skip


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(_: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            error_body(exc.code, exc.message, exc.details), exc.status, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", "")} for e in exc.errors()]
        return JSONResponse(error_body("validation_error", "Invalid request", details), 422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        msg = exc.detail if isinstance(exc.detail, str) else code.replace("_", " ")
        return JSONResponse(error_body(code, msg), exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(error_body("internal_error", "Internal server error"), 500)
