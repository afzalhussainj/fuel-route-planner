from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class GeoPoint:
    latitude: float
    longitude: float
    label: str = ""


@dataclass(frozen=True, slots=True)
class DrivingRoute:
    distance_meters: float
    duration_seconds: float
    # GeoJSON coordinates as [lon, lat]
    coordinates: list[list[float]]
    provider: str = "geoapify"
    provider_route_id: str | None = None

    @property
    def distance_miles(self) -> float:
        return self.distance_meters / 1609.344

    def to_geojson_feature(self) -> dict[str, Any]:
        return {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": self.coordinates,
            },
            "properties": {
                "distance_meters": self.distance_meters,
                "distance_miles": self.distance_miles,
                "duration_seconds": self.duration_seconds,
                "provider": self.provider,
            },
        }


class GeocodingProvider(Protocol):
    def geocode_us(self, text: str) -> GeoPoint: ...


class RoutingProvider(Protocol):
    def route(self, start: GeoPoint, finish: GeoPoint) -> DrivingRoute: ...
