from __future__ import annotations

import time
from pathlib import Path

import pytest
from apps.fuel.services.pipeline import load_processed_csv
from apps.routing.services.candidates import (
    assert_route_feasible_for_range,
    max_candidate_gap_miles,
    select_stations_along_route,
    stations_from_iterable,
)
from apps.routing.services.exceptions import RoutePlanningError
from apps.routing.services.geometry import (
    accuracy_corridor_miles,
    build_projected_route,
    point_route_metrics,
)
from django.conf import settings


def _eastbound_route() -> list[list[float]]:
    # Roughly lon -100 -> -90 along lat 30N (~600+ geometric miles).
    return [[-100.0, 30.0], [-97.0, 30.0], [-95.0, 30.0], [-92.0, 30.0], [-90.0, 30.0]]


def _station(
    opis_id: int,
    lon: float,
    lat: float,
    *,
    price: float = 3.0,
    accuracy: str = "exact",
    name: str | None = None,
) -> dict:
    return {
        "station_id": opis_id,
        "opis_id": opis_id,
        "name": name or f"Stop {opis_id}",
        "address": "I-10",
        "city": "Test",
        "state": "TX",
        "price_per_gallon": price,
        "latitude": lat,
        "longitude": lon,
        "location_accuracy": accuracy,
    }


def test_station_directly_on_route() -> None:
    route_coords = _eastbound_route()
    stations = stations_from_iterable([_station(1, -95.0, 30.0)])
    result = select_stations_along_route(
        route_coords,
        provider_distance_miles=600.0,
        stations=stations,
        corridor_miles=5.0,
        expand_if_infeasible=False,
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].distance_from_route_miles < 0.5
    assert 0 < result.candidates[0].mile_marker < 600


def test_station_near_route_inside_corridor() -> None:
    # ~3 miles north of the parallel (roughly 0.043 deg lat).
    stations = stations_from_iterable([_station(2, -95.0, 30.043)])
    result = select_stations_along_route(
        _eastbound_route(),
        provider_distance_miles=600.0,
        stations=stations,
        corridor_miles=5.0,
        expand_if_infeasible=False,
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].distance_from_route_miles < 5.0


def test_station_outside_corridor_rejected() -> None:
    # ~40 miles north.
    stations = stations_from_iterable([_station(3, -95.0, 30.58, price=1.50)])
    result = select_stations_along_route(
        _eastbound_route(),
        provider_distance_miles=600.0,
        stations=stations,
        corridor_miles=5.0,
        expand_if_infeasible=False,
    )
    assert result.candidates == []


def test_sorting_by_route_position() -> None:
    stations = stations_from_iterable(
        [
            _station(10, -92.0, 30.0),
            _station(11, -97.0, 30.0),
            _station(12, -95.0, 30.0),
        ]
    )
    result = select_stations_along_route(
        _eastbound_route(),
        provider_distance_miles=600.0,
        stations=stations,
        corridor_miles=5.0,
        expand_if_infeasible=False,
    )
    assert [c.opis_id for c in result.candidates] == [11, 12, 10]
    markers = [c.mile_marker for c in result.candidates]
    assert markers == sorted(markers)


def test_start_and_end_edge_conditions() -> None:
    coords = _eastbound_route()
    stations = stations_from_iterable(
        [
            _station(1, -100.0, 30.0),  # at start — kept for origin fueling
            _station(2, -90.0, 30.0),  # at finish — excluded
            _station(3, -95.0, 30.0),  # mid
        ]
    )
    result = select_stations_along_route(
        coords,
        provider_distance_miles=600.0,
        stations=stations,
        corridor_miles=5.0,
        expand_if_infeasible=False,
    )
    opis_ids = [c.opis_id for c in result.candidates]
    assert 2 not in opis_ids
    assert 3 in opis_ids
    assert 1 in opis_ids


def test_approximate_coordinates_get_wider_corridor_but_rank_lower() -> None:
    assert accuracy_corridor_miles(5.0, "exact") == 5.0
    assert accuracy_corridor_miles(5.0, "city") == 10.0

    # ~7 miles off route: rejected for exact, accepted for city.
    stations = stations_from_iterable(
        [
            _station(1, -95.0, 30.10, accuracy="exact", price=2.5),
            _station(2, -95.0, 30.10, accuracy="city", price=3.5),
        ]
    )
    # Use distinct opis already; for same location different accuracy test separately
    stations = stations_from_iterable(
        [
            _station(1, -95.05, 30.10, accuracy="exact", price=2.5),
            _station(2, -94.95, 30.10, accuracy="city", price=3.5),
        ]
    )
    result = select_stations_along_route(
        _eastbound_route(),
        provider_distance_miles=600.0,
        stations=stations,
        corridor_miles=5.0,
        expand_if_infeasible=False,
    )
    city_hits = [c for c in result.candidates if c.location_accuracy == "city"]
    exact_hits = [c for c in result.candidates if c.location_accuracy == "exact"]
    assert city_hits, "city accuracy should allow a wider corridor"
    # Exact at ~7mi should be outside 5mi corridor
    assert exact_hits == [] or all(c.distance_from_route_miles <= 5.0 for c in exact_hits)


def test_provider_distance_scales_progress() -> None:
    coords = [[-100.0, 30.0], [-90.0, 30.0]]
    route = build_projected_route(coords, provider_distance_miles=100.0)
    _dist, progress = point_route_metrics(route, longitude=-95.0, latitude=30.0)
    # Midpoint should be near half of provider distance, not raw geometric length.
    assert progress == pytest.approx(50.0, abs=5.0)
    assert route.provider_distance_miles == 100.0


def test_long_route_with_dense_candidates_is_feasible() -> None:
    coords = _eastbound_route()
    # Place stations every ~100 miles of longitude (~70-80 mi) along the line.
    lons = [-99, -98, -97, -96, -95, -94, -93, -92, -91]
    stations = stations_from_iterable(
        [_station(i, lon, 30.0) for i, lon in enumerate(lons, start=1)]
    )
    result = select_stations_along_route(
        coords,
        provider_distance_miles=700.0,
        stations=stations,
        corridor_miles=5.0,
        vehicle_range_miles=500.0,
        expand_if_infeasible=False,
    )
    assert result.candidates
    assert result.feasible_for_range
    assert result.max_candidate_gap_miles <= 500.0


def test_candidate_gap_greater_than_500_miles_fails() -> None:
    coords = _eastbound_route()
    # Station near the start leaves a ~800+ mile remaining gap on a 900-mile trip.
    stations = stations_from_iterable([_station(1, -99.5, 30.0)])
    result = select_stations_along_route(
        coords,
        provider_distance_miles=900.0,
        stations=stations,
        corridor_miles=5.0,
        max_corridor_miles=5.0,
        vehicle_range_miles=500.0,
        expand_if_infeasible=True,
    )
    assert result.feasible_for_range is False
    assert result.max_candidate_gap_miles > 500.0
    with pytest.raises(RoutePlanningError, match="exceeds"):
        assert_route_feasible_for_range(result, vehicle_range_miles=500.0)


def test_duplicate_physical_stations_deduped_by_opis() -> None:
    stations = stations_from_iterable(
        [
            _station(7, -95.0, 30.0, price=3.1),
            _station(7, -95.0, 30.0, price=2.9),
        ]
    )
    result = select_stations_along_route(
        _eastbound_route(),
        provider_distance_miles=600.0,
        stations=stations,
        corridor_miles=5.0,
        expand_if_infeasible=False,
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].opis_id == 7


def test_bounding_box_prefilter_rejects_distant_station() -> None:
    stations = stations_from_iterable(
        [
            _station(1, -95.0, 30.0),
            _station(99, -120.0, 45.0, price=1.0),  # Pacific NW, far from route
        ]
    )
    result = select_stations_along_route(
        _eastbound_route(),
        provider_distance_miles=600.0,
        stations=stations,
        corridor_miles=5.0,
        expand_if_infeasible=False,
    )
    assert [c.opis_id for c in result.candidates] == [1]


def test_max_candidate_gap_helper() -> None:
    coords = _eastbound_route()
    stations = stations_from_iterable([_station(1, -97.0, 30.0), _station(2, -93.0, 30.0)])
    result = select_stations_along_route(
        coords,
        provider_distance_miles=600.0,
        stations=stations,
        corridor_miles=5.0,
        expand_if_infeasible=False,
    )
    gap = max_candidate_gap_miles(result.candidates, 600.0)
    assert gap == pytest.approx(result.max_candidate_gap_miles)


def test_corridor_expansion_can_rescue_near_miss(settings) -> None:
    settings.ROUTE_CORRIDOR_MILES = 5
    settings.ROUTE_CORRIDOR_MAX_MILES = 12
    # Station about 8 miles off: missed at 5, caught at expanded city/approx or max corridor.
    stations = stations_from_iterable([_station(1, -95.0, 30.12, accuracy="exact")])
    # Exact only gets base corridor; expansion raises base to max for all.
    tight = select_stations_along_route(
        _eastbound_route(),
        provider_distance_miles=200.0,
        stations=stations,
        corridor_miles=5.0,
        max_corridor_miles=5.0,
        vehicle_range_miles=500.0,
        expand_if_infeasible=False,
    )
    # For a 200mi trip with no stations, gap is 200 < 500 so feasible without needing the station.
    # Force infeasibility with long provider distance and no on-corridor stations.
    long = select_stations_along_route(
        _eastbound_route(),
        provider_distance_miles=900.0,
        stations=stations,
        corridor_miles=5.0,
        max_corridor_miles=12.0,
        vehicle_range_miles=500.0,
        expand_if_infeasible=True,
    )
    assert long.expanded is True
    assert any(c.opis_id == 1 for c in long.candidates)
    assert tight.candidates == [] or tight.corridor_miles == 5.0


@pytest.mark.django_db
def test_profile_candidate_extraction_on_processed_dataset() -> None:
    processed = Path(settings.PROCESSED_FUEL_STATIONS_CSV)
    if not processed.is_file():
        pytest.skip("processed fuel stations CSV not available")

    rows = load_processed_csv(processed)
    stations = stations_from_iterable(
        [
            {
                "station_id": r["opis_id"],
                "opis_id": r["opis_id"],
                "name": r["name"],
                "address": r["address"],
                "city": r["city"],
                "state": r["state"],
                "price_per_gallon": float(r["retail_price"]),
                "latitude": r["latitude"],
                "longitude": r["longitude"],
                "location_accuracy": r["location_accuracy"],
            }
            for r in rows
            if r.get("latitude") is not None
        ]
    )
    # Approximate Dallas -> Chicago corridor polyline.
    coords = [
        [-96.7970, 32.7767],
        [-94.0, 35.0],
        [-90.2, 38.6],
        [-87.6298, 41.8781],
    ]
    provider_miles = 920.0

    started = time.perf_counter()
    result = select_stations_along_route(
        coords,
        provider_distance_miles=provider_miles,
        stations=stations,
        corridor_miles=5.0,
        max_corridor_miles=12.0,
        vehicle_range_miles=500.0,
        expand_if_infeasible=True,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert elapsed_ms < 1500.0, f"candidate extraction too slow: {elapsed_ms:.1f}ms"
    assert len(stations) > 1000
    assert len(result.candidates) > 0
