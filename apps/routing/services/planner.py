from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from apps.routing.providers.base import DrivingRoute, GeoPoint
from apps.routing.providers.geoapify import GeoapifyProvider
from apps.routing.services.candidates import (
    CandidateSelectionResult,
    assert_route_feasible_for_range,
    load_station_records_from_db,
    select_stations_along_route,
)
from apps.routing.services.exceptions import GeocodingError, RoutingError
from apps.routing.services.location import fetch_route, resolve_location
from apps.routing.services.optimizer import FuelPlan, plan_fuel_stops


@dataclass(frozen=True, slots=True)
class LocationInput:
    latitude: float | None = None
    longitude: float | None = None
    text: str | None = None
    raw_input: Any = None

    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    def has_text(self) -> bool:
        return bool(self.text and self.text.strip())

    def display_input(self) -> Any:
        if self.raw_input is not None:
            return self.raw_input
        if self.has_text():
            return self.text
        return {"latitude": self.latitude, "longitude": self.longitude}


@dataclass(frozen=True, slots=True)
class RouteResolution:
    start: GeoPoint
    finish: GeoPoint
    start_input: Any
    finish_input: Any
    route: DrivingRoute
    external_api_calls: int
    processing_ms: int
    timings_ms: dict[str, int]
    cache: dict[str, bool]
    candidates: CandidateSelectionResult
    fuel_plan: FuelPlan

    def to_api_dict(self) -> dict[str, Any]:
        summary = self.fuel_plan.to_api_summary()
        assumptions = _assumption_list(summary["assumptions"])
        return {
            "start": {
                "input": self.start_input,
                "latitude": self.start.latitude,
                "longitude": self.start.longitude,
                "label": self.start.label,
            },
            "finish": {
                "input": self.finish_input,
                "latitude": self.finish.latitude,
                "longitude": self.finish.longitude,
                "label": self.finish.label,
            },
            "route": {
                "distance_miles": round(self.route.distance_miles, 2),
                "duration_minutes": round(self.route.duration_seconds / 60.0, 1),
                "geometry": self.route.to_geojson_feature(),
            },
            "vehicle": {
                "mpg": summary["mpg"],
                "max_range_miles": summary["max_range_miles"],
                "tank_capacity_gallons": summary["tank_capacity_gallons"],
            },
            "fuel_plan": {
                "total_gallons": summary["total_gallons_required"],
                "total_gallons_purchased": summary["total_gallons_purchased"],
                "estimated_total_cost_usd": summary["estimated_fuel_cost_usd"],
                "stop_count": summary["fuel_stop_count"],
                "stops": [stop.to_api_dict() for stop in self.fuel_plan.stops],
            },
            "assumptions": assumptions,
            "meta": {
                "external_api_calls": self.external_api_calls,
                "processing_ms": self.processing_ms,
                "timings_ms": self.timings_ms,
                "fuel_planning": "cost_optimized",
                "cache": self.cache,
                "corridor_miles": self.candidates.corridor_miles,
                "corridor_expanded": self.candidates.expanded,
                "candidate_station_count": len(self.candidates.candidates),
                "max_candidate_gap_miles": round(self.candidates.max_candidate_gap_miles, 2),
                "route_feasible_for_range": self.candidates.feasible_for_range,
            },
        }


def _assumption_list(raw: dict[str, Any]) -> list[str]:
    ordered = [
        ("starting_fuel", raw.get("starting_fuel")),
        (
            "origin_station",
            (
                f"Origin fueling station OPIS ID: {raw['origin_station_opis_id']}."
                if raw.get("origin_station_opis_id") is not None
                else None
            ),
        ),
        (
            "origin_search",
            (
                f"Origin station search window: {raw['origin_search_miles']} "
                "miles of route progress."
                if raw.get("origin_search_miles") is not None
                else None
            ),
        ),
        ("detour_model", raw.get("detour_model")),
        ("algorithm", raw.get("algorithm")),
        (
            "vehicle",
            (
                f"Vehicle model: {raw.get('vehicle_mpg')} MPG, "
                f"{raw.get('vehicle_max_range_miles')} mile max range, "
                f"{raw.get('tank_capacity_gallons')} gallon usable tank."
                if raw.get("vehicle_mpg") is not None
                else None
            ),
        ),
    ]
    return [text for _, text in ordered if text]


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


class RoutePlanningService:
    """
    Resolve start/finish, driving route, corridor candidates, and a cost-optimized
    fuel plan for the fixed route.
    """

    def __init__(
        self,
        provider: GeoapifyProvider | None = None,
        *,
        stations: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        self._provider = provider
        self._owns_provider = provider is None
        self._stations = stations

    def plan(self, start: LocationInput, finish: LocationInput) -> dict[str, Any]:
        started = time.perf_counter()
        provider = self._provider or GeoapifyProvider()
        try:
            geocode_started = time.perf_counter()
            start_point, _, start_cache_hit = resolve_location(
                text=start.text,
                latitude=start.latitude,
                longitude=start.longitude,
                field_name="start",
                provider=provider,
            )
            finish_point, _, finish_cache_hit = resolve_location(
                text=finish.text,
                latitude=finish.latitude,
                longitude=finish.longitude,
                field_name="finish",
                provider=provider,
            )
            geocode_ms = _elapsed_ms(geocode_started)
            if _same_point(start_point, finish_point):
                raise GeocodingError(
                    "Start and finish resolve to the same location; provide two distinct places."
                )

            route_started = time.perf_counter()
            route, route_cache_hit = fetch_route(
                start_point,
                finish_point,
                provider=provider,
            )
            route_ms = _elapsed_ms(route_started)
            if route.distance_miles < 0.1:
                raise RoutingError(
                    "Routing provider returned a negligible route between start and finish."
                )

            candidates_started = time.perf_counter()
            stations = (
                list(self._stations)
                if self._stations is not None
                else load_station_records_from_db()
            )
            candidates = select_stations_along_route(
                route.coordinates,
                provider_distance_miles=route.distance_miles,
                stations=stations,
            )
            assert_route_feasible_for_range(candidates)
            candidates_ms = _elapsed_ms(candidates_started)

            optimizer_started = time.perf_counter()
            fuel_plan = plan_fuel_stops(
                candidates.candidates,
                route_distance_miles=route.distance_miles,
            )
            optimizer_ms = _elapsed_ms(optimizer_started)

            processing_ms = _elapsed_ms(started)
            timings_ms = {
                "geocode": geocode_ms,
                "route": route_ms,
                "candidates": candidates_ms,
                "optimizer": optimizer_ms,
                "total": processing_ms,
            }
            result = RouteResolution(
                start=start_point,
                finish=finish_point,
                start_input=start.display_input(),
                finish_input=finish.display_input(),
                route=route,
                external_api_calls=provider.external_api_calls,
                processing_ms=processing_ms,
                timings_ms=timings_ms,
                cache={
                    "geocode_start": start_cache_hit,
                    "geocode_finish": finish_cache_hit,
                    "route": route_cache_hit,
                },
                candidates=candidates,
                fuel_plan=fuel_plan,
            )
            return result.to_api_dict()
        finally:
            if self._owns_provider:
                provider.close()


def _same_point(a: GeoPoint, b: GeoPoint, *, epsilon: float = 1e-4) -> bool:
    return abs(a.latitude - b.latitude) < epsilon and abs(a.longitude - b.longitude) < epsilon
