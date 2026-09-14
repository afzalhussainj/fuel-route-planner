from __future__ import annotations

from rest_framework import status

from apps.common.exceptions import APIError


class FuelRouteError(Exception):
    """Base error for fuel-route domain failures."""

    code = "request_error"
    http_status = status.HTTP_400_BAD_REQUEST

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def to_api_error(self) -> APIError:
        return APIError(
            code=self.code,
            message=self.message,
            http_status=self.http_status,
        )


class ConfigurationError(FuelRouteError):
    code = "service_unavailable"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE


class GeocodingError(FuelRouteError):
    code = "invalid_location"
    http_status = status.HTTP_400_BAD_REQUEST


class LocationNotFoundError(GeocodingError):
    code = "location_not_found"


class AmbiguousLocationError(GeocodingError):
    code = "ambiguous_location"


class RoutingError(FuelRouteError):
    code = "routing_failed"
    http_status = status.HTTP_502_BAD_GATEWAY


class RoutePlanningError(FuelRouteError):
    code = "route_planning_failed"
    http_status = status.HTTP_422_UNPROCESSABLE_ENTITY


class ProviderTimeoutError(FuelRouteError):
    code = "upstream_timeout"
    http_status = status.HTTP_504_GATEWAY_TIMEOUT


class ProviderRateLimitError(FuelRouteError):
    code = "rate_limited"
    http_status = status.HTTP_429_TOO_MANY_REQUESTS


class ProviderUpstreamError(FuelRouteError):
    code = "upstream_error"
    http_status = status.HTTP_502_BAD_GATEWAY
