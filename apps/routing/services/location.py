from __future__ import annotations

import hashlib
import logging
from typing import Any

from django.conf import settings
from django.core.cache import cache

from apps.routing.providers.base import DrivingRoute, GeocodingProvider, GeoPoint
from apps.routing.providers.geoapify import validate_us_coordinates
from apps.routing.services.exceptions import GeocodingError

logger = logging.getLogger(__name__)


def normalize_location_text(text: str) -> str:
    return " ".join(text.strip().casefold().split())


def geocode_cache_key(text: str) -> str:
    normalized = normalize_location_text(text)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"geocode:v1:us:{digest}"


def route_cache_key(start: GeoPoint, finish: GeoPoint) -> str:
    payload = (
        f"{start.latitude:.5f},{start.longitude:.5f}|"
        f"{finish.latitude:.5f},{finish.longitude:.5f}|drive"
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"route:v1:{digest}"


def geocode_point(
    text: str,
    *,
    provider: GeocodingProvider,
    use_cache: bool = True,
) -> tuple[GeoPoint, bool]:
    """Return (point, cache_hit)."""
    key = geocode_cache_key(text)
    if use_cache:
        cached = cache.get(key)
        if isinstance(cached, dict) and "latitude" in cached and "longitude" in cached:
            return (
                GeoPoint(
                    latitude=float(cached["latitude"]),
                    longitude=float(cached["longitude"]),
                    label=str(cached.get("label") or text),
                ),
                True,
            )

    point = provider.geocode_us(text)
    cache.set(
        key,
        {
            "latitude": point.latitude,
            "longitude": point.longitude,
            "label": point.label,
        },
        timeout=int(settings.GEOCODE_CACHE_TTL_SECONDS),
    )
    return point, False


def resolve_location(
    *,
    text: str | None,
    latitude: float | None,
    longitude: float | None,
    field_name: str,
    provider: GeocodingProvider,
    use_cache: bool = True,
) -> tuple[GeoPoint, bool, bool]:
    """
    Resolve a location input.

    Returns (point, did_geocode_call_path, cache_hit).
    Coordinate inputs never call the geocoder (cache_hit=False, did_geocode=False).
    """
    if latitude is not None and longitude is not None:
        validate_us_coordinates(latitude, longitude)
        label = text.strip() if text else field_name
        return GeoPoint(latitude, longitude, label=label), False, False

    if text and text.strip():
        point, cache_hit = geocode_point(text, provider=provider, use_cache=use_cache)
        return point, True, cache_hit

    raise GeocodingError(
        f"{field_name} requires either a text location string or latitude/longitude."
    )


def fetch_route(
    start: GeoPoint,
    finish: GeoPoint,
    *,
    provider: Any,
    use_cache: bool = True,
) -> tuple[DrivingRoute, bool]:
    key = route_cache_key(start, finish)
    if use_cache:
        cached = cache.get(key)
        if isinstance(cached, dict):
            try:
                return (
                    DrivingRoute(
                        distance_meters=float(cached["distance_meters"]),
                        duration_seconds=float(cached["duration_seconds"]),
                        coordinates=list(cached["coordinates"]),
                        provider=str(cached.get("provider") or "geoapify"),
                        provider_route_id=cached.get("provider_route_id"),
                    ),
                    True,
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("Ignoring malformed route cache entry for %s", key)

    route = provider.route(start, finish)
    cache.set(
        key,
        {
            "distance_meters": route.distance_meters,
            "duration_seconds": route.duration_seconds,
            "coordinates": route.coordinates,
            "provider": route.provider,
            "provider_route_id": route.provider_route_id,
        },
        timeout=int(settings.ROUTE_CACHE_TTL_SECONDS),
    )
    return route, False
