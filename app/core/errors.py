"""Errors and the single error envelope.

Every failure the API returns has the same shape:

```json
{"error": {"code": "not_found", "message": "Post not found", "details": {}}}
```

Clients switch on `code`, which is stable and machine-readable; `message` is for
developers and logs, not for display. The Flutter app renders its own text from
the code, which also keeps English and Swahili out of the backend.

Service code raises `AppError` subclasses. The handlers registered by
`register_exception_handlers` turn those — and validation errors, Starlette's
`HTTPException`, and anything unhandled — into the envelope.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette import status
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import REQUEST_ID_HEADER

logger = logging.getLogger(__name__)


class ErrorCode(StrEnum):
    """Stable error codes. Add to this; never repurpose an existing value."""

    # 400 / 422
    bad_request = "bad_request"
    validation_error = "validation_error"
    invalid_cursor = "invalid_cursor"

    # 401 / 403
    unauthenticated = "unauthenticated"
    forbidden = "forbidden"

    # 404 / 405 / 409
    not_found = "not_found"
    method_not_allowed = "method_not_allowed"
    conflict = "conflict"

    # 413 / 415 / 429
    payload_too_large = "payload_too_large"
    unsupported_media_type = "unsupported_media_type"
    rate_limited = "rate_limited"

    # 5xx
    internal_error = "internal_error"
    service_unavailable = "service_unavailable"


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    """The response model for every error, so it shows up in the OpenAPI schema."""

    error: ErrorBody

    model_config = {
        "json_schema_extra": {
            "example": {
                "error": {
                    "code": "not_found",
                    "message": "Resource not found",
                    "details": {},
                }
            }
        }
    }


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class AppError(Exception):
    """Base class for errors the API returns deliberately.

    Subclasses set the status and code; call sites override the message and add
    `details` when the client can act on the specifics.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: ErrorCode = ErrorCode.internal_error
    message: str = "Something went wrong"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.details: dict[str, Any] = details or {}
        self.headers = headers or {}
        super().__init__(self.message)

    def to_envelope(self) -> ErrorEnvelope:
        return ErrorEnvelope(
            error=ErrorBody(code=self.code, message=self.message, details=self.details)
        )


class BadRequestError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = ErrorCode.bad_request
    message = "Bad request"


class InvalidCursorError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = ErrorCode.invalid_cursor
    message = "The pagination cursor is not valid"


class UnauthenticatedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = ErrorCode.unauthenticated
    message = "Authentication required"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            message, details=details, headers={"WWW-Authenticate": "Bearer"} | (headers or {})
        )


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = ErrorCode.forbidden
    message = "You do not have access to this"


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = ErrorCode.not_found
    message = "Resource not found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = ErrorCode.conflict
    message = "The request conflicts with the current state"


class RateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = ErrorCode.rate_limited
    message = "Too many requests"

    def __init__(
        self,
        message: str | None = None,
        *,
        retry_after_seconds: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            details={"retry_after_seconds": retry_after_seconds, **(details or {})},
            headers={"Retry-After": str(retry_after_seconds)},
        )


class ServiceUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = ErrorCode.service_unavailable
    message = "The service is temporarily unavailable"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
_STATUS_TO_CODE: dict[int, ErrorCode] = {
    status.HTTP_400_BAD_REQUEST: ErrorCode.bad_request,
    status.HTTP_401_UNAUTHORIZED: ErrorCode.unauthenticated,
    status.HTTP_403_FORBIDDEN: ErrorCode.forbidden,
    status.HTTP_404_NOT_FOUND: ErrorCode.not_found,
    status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.method_not_allowed,
    status.HTTP_409_CONFLICT: ErrorCode.conflict,
    status.HTTP_413_CONTENT_TOO_LARGE: ErrorCode.payload_too_large,
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: ErrorCode.unsupported_media_type,
    status.HTTP_422_UNPROCESSABLE_CONTENT: ErrorCode.validation_error,
    status.HTTP_429_TOO_MANY_REQUESTS: ErrorCode.rate_limited,
    status.HTTP_503_SERVICE_UNAVAILABLE: ErrorCode.service_unavailable,
}


def _envelope_response(
    request: Request,
    status_code: int,
    code: ErrorCode,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorEnvelope(error=ErrorBody(code=code, message=message, details=details or {}))
    response_headers = dict(headers or {})
    # Read from the scope rather than the context variable: an unhandled exception
    # is turned into a response by Starlette's outermost middleware, by which point
    # the request-scoped context variable has already been reset. The header is what
    # lets a user reporting a failure be matched to the log line that explains it.
    request_id = request.scope.get("state", {}).get("request_id")
    if request_id:
        response_headers[REQUEST_ID_HEADER] = str(request_id)
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=response_headers or None,
    )


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)  # noqa: S101 — registered for this type only
    # Deliberate errors are expected traffic, not incidents: log at info so they
    # stay searchable without drowning real problems.
    logger.info(
        "request_failed",
        extra={"error_code": str(exc.code), "status_code": exc.status_code},
    )
    return _envelope_response(
        request, exc.status_code, exc.code, exc.message, exc.details, exc.headers
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    # Only the location and reason are reported, never the offending input. Clients
    # know what they sent, and omitting it keeps coordinates out of error payloads
    # and logs — the brief forbids coordinates being logged at all.
    fields = [
        {
            "location": list(error.get("loc", ())),
            "type": error.get("type", "invalid"),
            "message": error.get("msg", "Invalid value"),
        }
        for error in exc.errors()
    ]
    return _envelope_response(
        request,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorCode.validation_error,
        "The request body or parameters are not valid",
        {"fields": fields},
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    code = _STATUS_TO_CODE.get(
        exc.status_code,
        ErrorCode.internal_error if exc.status_code >= 500 else ErrorCode.bad_request,
    )
    detail = exc.detail if isinstance(exc.detail, str) else None
    headers = dict(exc.headers) if exc.headers else None
    return _envelope_response(
        request, exc.status_code, code, detail or code.replace("_", " "), None, headers
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last resort: log the traceback, return a generic envelope.

    Internal details never reach the client, in any environment — a stack trace
    tells an attacker about the library versions and file layout, and debugging
    belongs in the logs where the request ID ties it back together.
    """
    logger.exception("unhandled_exception", extra={"path": request.url.path})
    return _envelope_response(
        request,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        ErrorCode.internal_error,
        "Something went wrong",
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
