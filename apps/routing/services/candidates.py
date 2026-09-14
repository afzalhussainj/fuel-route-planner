from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from django.conf import settings

from apps.routing.services.exceptions import RoutePlanningError
from apps.routing.services.geometry import (
    ProjectedRoute,
    accuracy_corridor_miles,
    accuracy_rank,
    build_projected_route,
    geographic_bbox_with_padding,
    point_route_metrics,
)

logger = logging.getLogger(__name__)

# Process-local station snapshot. Cleared after imports and in tests.
# Avoids reloading ~6k rows on every plan request without Redis.
_station_records: list[dict[str, Any]] | None = None


@dataclass(frozen=True, slots=True)
class StationCandidate:
    station_id: int
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    price_per_gallon: float
    latitude: float
    longitude: float
    location_accuracy: str | None
    mile_marker: float
    distance_from_route_miles: float

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["price_per_gallon"] = round(self.price_per_gallon, 6)
        payload["mile_marker"] = round(self.mile_marker, 3)
        payload["distance_from_route_miles"] = round(self.distance_from_route_miles, 3)
        return payload


@dataclass(frozen=True, slots=True)
class CandidateSelectionResult:
    candidates: list[StationCandidate]
    corridor_miles: float
    expanded: bool
    provider_distance_miles: float
    feasible_for_range: bool
    max_candidate_gap_miles: float


def clear_station_records_cache() -> None:
    """Drop the in-process station snapshot (tests / after imports)."""
    global _station_records
    _station_records = None


def load_station_records_from_db(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    """
    Load optimizer fields for geocoded stations in a single query.

    Results are cached in-process until clear_station_records_cache().
    Corridor matching never hits Geoapify.
    """
    global _station_records
    if _station_records is not None and not force_refresh:
        return list(_station_records)

    from apps.fuel.models import FuelStation

    rows = FuelStation.objects.filter(
        latitude__isnull=False,
        longitude__isnull=False,
    ).values(
        "id",
        "opis_id",
        "name",
        "address",
        "city",
        "state",
        "retail_price",
        "latitude",
        "longitude",
        "location_accuracy",
    )
    _station_records = [
        {
            "station_id": int(row["id"]),
            "opis_id": int(row["opis_id"]),
            "name": row["name"],
            "address": row["address"],
            "city": row["city"],
            "state": row["state"],
            "price_per_gallon": float(row["retail_price"]),
            "latitude": float(row["latitude"]),
            "longitude": float(row["longitude"]),
            "location_accuracy": row["location_accuracy"],
        }
        for row in rows
    ]
    return list(_station_records)


def select_stations_along_route(
    coordinates_lonlat: list[list[float]],
    *,
    provider_distance_miles: float,
    stations: Sequence[dict[str, Any]],
    corridor_miles: float | None = None,
    max_corridor_miles: float | None = None,
    vehicle_range_miles: float | None = None,
    expand_if_infeasible: bool = True,
) -> CandidateSelectionResult:
    """
    Identify fuel stations inside a route corridor without extra routing API calls.

    Provider distance remains authoritative. Geometric progress is scaled to it.
    If candidate gaps exceed vehicle range, optionally expand the corridor once
    up to `max_corridor_miles` before declaring the route infeasible.
    """
    base_corridor = float(
        corridor_miles if corridor_miles is not None else settings.ROUTE_CORRIDOR_MILES
    )
    max_corridor = float(
        max_corridor_miles if max_corridor_miles is not None else settings.ROUTE_CORRIDOR_MAX_MILES
    )
    range_miles = float(
        vehicle_range_miles if vehicle_range_miles is not None else settings.VEHICLE_MAX_RANGE_MILES
    )
    if base_corridor <= 0:
        raise ValueError("corridor_miles must be positive")
    if max_corridor < base_corridor:
        max_corridor = base_corridor

    route = build_projected_route(
        coordinates_lonlat,
        provider_distance_miles=provider_distance_miles,
    )

    candidates = _candidates_for_corridor(route, stations, base_corridor)
    max_gap = max_candidate_gap_miles(candidates, route.provider_distance_miles)
    expanded = False

    if expand_if_infeasible and max_gap > range_miles and max_corridor > base_corridor:
        logger.info(
            "Candidate gap %.1f mi exceeds range %.1f mi; expanding corridor from %.1f to %.1f mi",
            max_gap,
            range_miles,
            base_corridor,
            max_corridor,
        )
        candidates = _candidates_for_corridor(route, stations, max_corridor)
        max_gap = max_candidate_gap_miles(candidates, route.provider_distance_miles)
        expanded = True
        base_corridor = max_corridor

    feasible = max_gap <= range_miles + 1e-6
    return CandidateSelectionResult(
        candidates=candidates,
        corridor_miles=base_corridor,
        expanded=expanded,
        provider_distance_miles=route.provider_distance_miles,
        feasible_for_range=feasible,
        max_candidate_gap_miles=max_gap,
    )


def assert_route_feasible_for_range(
    result: CandidateSelectionResult,
    *,
    vehicle_range_miles: float | None = None,
) -> None:
    range_miles = float(
        vehicle_range_miles if vehicle_range_miles is not None else settings.VEHICLE_MAX_RANGE_MILES
    )
    if result.feasible_for_range:
        return
    raise RoutePlanningError(
        f"No fuel-station coverage for this route within a "
        f"{result.corridor_miles:.1f}-mile corridor: largest gap is "
        f"{result.max_candidate_gap_miles:.1f} miles, which exceeds the "
        f"{range_miles:.0f}-mile vehicle range."
    )


def max_candidate_gap_miles(
    candidates: Sequence[StationCandidate],
    provider_distance_miles: float,
) -> float:
    """
    Largest distance between consecutive refueling opportunities along the route,
    including start (mile 0) and destination (provider distance).
    """
    markers = [0.0]
    markers.extend(c.mile_marker for c in candidates)
    markers.append(float(provider_distance_miles))
    if len(markers) < 2:
        return float(provider_distance_miles)
    return max(markers[i + 1] - markers[i] for i in range(len(markers) - 1))


def _candidates_for_corridor(
    route: ProjectedRoute,
    stations: Sequence[dict[str, Any]],
    corridor_miles: float,
) -> list[StationCandidate]:
    # Pad bbox with the widest accuracy-adjusted corridor we might accept.
    west, south, east, north = geographic_bbox_with_padding(
        route.projected,
        padding_miles=corridor_miles * 2.0,
    )

    seen_opis: set[int] = set()
    matched: list[StationCandidate] = []

    for station in stations:
        try:
            lon = float(station["longitude"])
            lat = float(station["latitude"])
            opis_id = int(station["opis_id"])
        except (KeyError, TypeError, ValueError):
            continue

        if opis_id in seen_opis:
            continue
        if lon < west or lon > east or lat < south or lat > north:
            continue

        accuracy = station.get("location_accuracy")
        allowed = accuracy_corridor_miles(corridor_miles, accuracy)
        distance_miles, progress_miles = point_route_metrics(route, longitude=lon, latitude=lat)
        if distance_miles > allowed + 1e-9:
            continue

        # Exclude destination-area stations; keep near-start stations for origin fueling.
        if progress_miles < 0 or progress_miles >= route.provider_distance_miles - 1e-6:
            continue

        seen_opis.add(opis_id)
        matched.append(
            StationCandidate(
                station_id=int(station.get("station_id") or opis_id),
                opis_id=opis_id,
                name=str(station.get("name") or ""),
                address=str(station.get("address") or ""),
                city=str(station.get("city") or ""),
                state=str(station.get("state") or ""),
                price_per_gallon=float(station["price_per_gallon"]),
                latitude=lat,
                longitude=lon,
                location_accuracy=str(accuracy) if accuracy else None,
                mile_marker=progress_miles,
                distance_from_route_miles=distance_miles,
            )
        )

    matched.sort(
        key=lambda c: (
            c.mile_marker,
            accuracy_rank(c.location_accuracy),
            c.distance_from_route_miles,
            c.price_per_gallon,
            c.opis_id,
        )
    )
    return matched


def stations_from_iterable(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize arbitrary station dicts for tests and offline profiling."""
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if row.get("latitude") is None or row.get("longitude") is None:
            continue
        normalized.append(
            {
                "station_id": int(row.get("station_id") or row.get("opis_id")),
                "opis_id": int(row["opis_id"]),
                "name": str(row.get("name") or ""),
                "address": str(row.get("address") or ""),
                "city": str(row.get("city") or ""),
                "state": str(row.get("state") or ""),
                "price_per_gallon": float(row.get("price_per_gallon", row.get("retail_price"))),
                "latitude": float(row["latitude"]),
                "longitude": float(row["longitude"]),
                "location_accuracy": row.get("location_accuracy"),
            }
        )
    return normalized
