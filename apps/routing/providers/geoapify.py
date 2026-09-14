from __future__ import annotations

import logging
from typing import Any

import httpx
from django.conf import settings

from apps.routing.providers.base import DrivingRoute, GeoPoint
from apps.routing.services.exceptions import (
    AmbiguousLocationError,
    ConfigurationError,
    GeocodingError,
    LocationNotFoundError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUpstreamError,
    RoutingError,
)

logger = logging.getLogger(__name__)

# Rough continental USA bounding box used only for coordinate inputs.
_US_LAT_MIN, _US_LAT_MAX = 24.0, 50.0
_US_LON_MIN, _US_LON_MAX = -125.0, -66.0


class GeoapifyProvider:
    """Geoapify geocoding + driving routing with a shared httpx client."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        client: httpx.Client | None = None,
        call_counter: list[int] | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.GEOAPIFY_API_KEY
        self.base_url = (base_url or settings.GEOAPIFY_BASE_URL).rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._call_counter = call_counter if call_counter is not None else [0]

        if not self.api_key:
            raise ConfigurationError(
                "GEOAPIFY_API_KEY is not set. Copy .env.example to .env and add a key."
            )

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    @property
    def external_api_calls(self) -> int:
        return self._call_counter[0]

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            from apps.routing.services.http_client import build_httpx_client

            self._client = build_httpx_client()
            self._owns_client = True
        return self._client

    def geocode_us(self, text: str) -> GeoPoint:
        query = text.strip()
        if not query:
            raise GeocodingError("Location text must not be empty.")

        payload = self._request_json(
            "GET",
            f"{self.base_url}/v1/geocode/search",
            params={
                "text": query,
                "filter": "countrycode:us",
                "limit": 5,
                "format": "json",
                "apiKey": self.api_key,
            },
        )
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not results:
            raise LocationNotFoundError(f"No US location could be resolved for '{query}'.")

        us_results = [
            hit for hit in results if str(hit.get("country_code") or "").lower() in {"", "us"}
        ]
        if not us_results:
            raise GeocodingError(f"Location '{query}' resolved outside the United States.")

        best = us_results[0]
        if len(us_results) > 1 and _is_ambiguous(us_results):
            raise AmbiguousLocationError(
                f"Location '{query}' is ambiguous; please provide a more specific "
                "US place name or coordinates."
            )

        try:
            lat = float(best["lat"])
            lon = float(best["lon"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderUpstreamError(
                "Geocoding provider returned a malformed coordinate payload."
            ) from exc

        label = str(best.get("formatted") or query)
        return GeoPoint(latitude=lat, longitude=lon, label=label)

    def route(self, start: GeoPoint, finish: GeoPoint) -> DrivingRoute:
        waypoints = f"{start.latitude},{start.longitude}|{finish.latitude},{finish.longitude}"
        payload = self._request_json(
            "GET",
            f"{self.base_url}/v1/routing",
            params={
                "waypoints": waypoints,
                "mode": "drive",
                "apiKey": self.api_key,
            },
        )
        features = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list) or not features:
            raise RoutingError("Routing provider returned no route features.")

        feature = features[0]
        if not isinstance(feature, dict):
            raise ProviderUpstreamError("Routing provider returned a malformed feature.")

        properties = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        distance_m = properties.get("distance")
        if distance_m is None and isinstance(properties.get("legs"), list):
            distance_m = sum(float(leg.get("distance") or 0) for leg in properties["legs"])
        if distance_m is None:
            raise ProviderUpstreamError("Routing response did not include distance.")

        coordinates = _extract_coordinates(geometry)
        if len(coordinates) < 2:
            raise RoutingError("Routing response did not include a usable geometry.")

        return DrivingRoute(
            distance_meters=float(distance_m),
            duration_seconds=float(properties.get("time") or 0),
            coordinates=coordinates,
            provider="geoapify",
            provider_route_id=str(properties.get("route_id") or "") or None,
        )

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self._call_counter[0] += 1
        from apps.routing.services.http_client import request_with_retries

        try:
            response = request_with_retries(
                self.client,
                method,
                url,
                params=params,
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("Upstream location/routing request timed out.") from exc
        except httpx.HTTPError as exc:
            raise ProviderUpstreamError("Upstream location/routing request failed.") from exc

        if response.status_code == 429:
            raise ProviderRateLimitError(
                "Upstream location/routing provider rate-limited the request."
            )
        if response.status_code >= 500:
            raise ProviderUpstreamError(f"Upstream provider error (HTTP {response.status_code}).")
        if response.status_code >= 400:
            # Avoid leaking API keys from query strings in error text.
            raise ProviderUpstreamError(
                f"Upstream provider rejected the request (HTTP {response.status_code})."
            )

        try:
            return response.json()
        except ValueError as exc:
            raise ProviderUpstreamError("Upstream provider returned malformed JSON.") from exc


def validate_us_coordinates(latitude: float, longitude: float) -> None:
    if not (_US_LAT_MIN <= latitude <= _US_LAT_MAX and _US_LON_MIN <= longitude <= _US_LON_MAX):
        raise GeocodingError("Coordinates fall outside the continental United States bounding box.")


def _is_ambiguous(results: list[dict[str, Any]]) -> bool:
    """Treat near-tied top results with distant coordinates as ambiguous."""
    if len(results) < 2:
        return False
    top = results[0]
    second = results[1]
    try:
        c1 = float((top.get("rank") or {}).get("confidence") or 0)
        c2 = float((second.get("rank") or {}).get("confidence") or 0)
        lat1, lon1 = float(top["lat"]), float(top["lon"])
        lat2, lon2 = float(second["lat"]), float(second["lon"])
    except (KeyError, TypeError, ValueError):
        return False
    # Same confidence band and clearly different places.
    if abs(c1 - c2) <= 0.05 and (abs(lat1 - lat2) > 0.2 or abs(lon1 - lon2) > 0.2):
        return True
    return False


def _extract_coordinates(geometry: dict[str, Any]) -> list[list[float]]:
    geom_type = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return []
    if geom_type == "LineString":
        return [[float(c[0]), float(c[1])] for c in coords]
    if geom_type == "MultiLineString":
        flattened: list[list[float]] = []
        for line in coords:
            for c in line:
                point = [float(c[0]), float(c[1])]
                if not flattened or flattened[-1] != point:
                    flattened.append(point)
        return flattened
    return []
