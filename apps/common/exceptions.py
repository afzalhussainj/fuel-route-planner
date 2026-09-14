from __future__ import annotations

import logging
from typing import Any

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


class APIError(Exception):
    """Domain/API error rendered into the standard JSON error envelope."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        http_status: int = status.HTTP_400_BAD_REQUEST,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def error_body(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def api_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    if isinstance(exc, APIError):
        return Response(
            error_body(exc.code, exc.message),
            status=exc.http_status,
        )

    # Domain errors from routing/fuel packages.
    to_api_error = getattr(exc, "to_api_error", None)
    if callable(to_api_error):
        api_error = to_api_error()
        if isinstance(api_error, APIError):
            return Response(
                error_body(api_error.code, api_error.message),
                status=api_error.http_status,
            )

    response = drf_exception_handler(exc, context)
    if response is not None:
        message = _message_from_drf_response(response.data)
        code = _code_for_status(response.status_code)
        response.data = error_body(code, message)
        return response

    logger.exception("Unhandled API exception", exc_info=exc)
    return Response(
        error_body("internal_error", "An unexpected error occurred."),
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def _code_for_status(http_status: int) -> str:
    mapping = {
        status.HTTP_400_BAD_REQUEST: "validation_error",
        status.HTTP_401_UNAUTHORIZED: "unauthorized",
        status.HTTP_403_FORBIDDEN: "forbidden",
        status.HTTP_404_NOT_FOUND: "not_found",
        status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
        status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
        status.HTTP_501_NOT_IMPLEMENTED: "not_implemented",
        status.HTTP_502_BAD_GATEWAY: "upstream_error",
        status.HTTP_503_SERVICE_UNAVAILABLE: "service_unavailable",
    }
    return mapping.get(http_status, "request_error")


def _message_from_drf_response(data: Any) -> str:
    if isinstance(data, dict):
        if "detail" in data:
            return str(data["detail"])
        parts: list[str] = []
        for key, value in data.items():
            if isinstance(value, list):
                parts.append(f"{key}: {'; '.join(str(item) for item in value)}")
            else:
                parts.append(f"{key}: {value}")
        if parts:
            return "; ".join(parts)
    if isinstance(data, list):
        return "; ".join(str(item) for item in data)
    return str(data)
