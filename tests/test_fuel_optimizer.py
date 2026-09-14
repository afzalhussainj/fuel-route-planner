from __future__ import annotations

from decimal import Decimal

import pytest
from apps.routing.services.candidates import StationCandidate
from apps.routing.services.exceptions import RoutePlanningError
from apps.routing.services.optimizer import plan_fuel_stops


def _c(
    opis_id: int,
    mile: float,
    price: float,
    *,
    detour: float = 0.0,
    name: str | None = None,
) -> StationCandidate:
    return StationCandidate(
        station_id=opis_id,
        opis_id=opis_id,
        name=name or f"S{opis_id}",
        address="Addr",
        city="City",
        state="TX",
        price_per_gallon=price,
        latitude=30.0,
        longitude=-97.0 - mile / 60.0,
        location_accuracy="exact",
        mile_marker=mile,
        distance_from_route_miles=detour,
    )


def test_under_500_mile_trip_has_origin_fuel_cost() -> None:
    candidates = [_c(1, 5.0, 3.00)]
    plan = plan_fuel_stops(candidates, route_distance_miles=200.0)
    assert len(plan.stops) == 1
    assert plan.stops[0].is_origin_fueling is True
    assert plan.total_gallons_required == Decimal("20")  # 200/10
    assert plan.estimated_fuel_cost > 0
    # Zero detour: purchased gallons match baseline route fuel.
    assert plan.total_gallons_purchased == Decimal("20")
    assert plan.stops[0].estimated_cost == Decimal("60.00")


def test_exactly_500_miles() -> None:
    candidates = [_c(1, 10.0, 3.00)]
    plan = plan_fuel_stops(candidates, route_distance_miles=500.0)
    assert plan.total_gallons_required == Decimal("50")
    assert len(plan.stops) == 1
    assert plan.stops[0].gallons_to_buy == Decimal("50")


def test_just_over_500_requires_en_route_stop() -> None:
    candidates = [_c(1, 5.0, 3.50), _c(2, 300.0, 3.00)]
    plan = plan_fuel_stops(candidates, route_distance_miles=501.0)
    assert len(plan.stops) >= 2
    assert plan.stops[0].is_origin_fueling is True
    assert any(not s.is_origin_fueling for s in plan.stops)
    assert plan.total_gallons_required == Decimal("50.1")


def test_multi_stop_trip() -> None:
    candidates = [
        _c(1, 10.0, 3.20),
        _c(2, 250.0, 3.00),
        _c(3, 500.0, 3.10),
        _c(4, 750.0, 2.90),
    ]
    plan = plan_fuel_stops(candidates, route_distance_miles=1000.0)
    assert len(plan.stops) >= 3
    assert plan.total_gallons_required == Decimal("100")
    markers = [s.route_mile for s in plan.stops]
    assert markers == sorted(markers)


def test_cheaper_farther_station_preferred_when_feasible() -> None:
    candidates = [
        _c(1, 5.0, 4.00),
        _c(2, 100.0, 3.80),  # expensive near
        _c(3, 400.0, 2.50),  # cheap farther, still reachable from origin
    ]
    plan = plan_fuel_stops(candidates, route_distance_miles=450.0)
    # Origin buys enough to reach destination or a stop; cheapest path should
    # fuel at origin then optionally the cheap station.
    prices_used = [s.price_per_gallon for s in plan.stops]
    assert Decimal("2.50") in prices_used or plan.stops[0].price_per_gallon == Decimal("4.00")
    # Cost should be less than fueling entire trip at $4
    assert plan.estimated_fuel_cost < Decimal("4.00") * Decimal("45")


def test_cheap_station_that_produces_infeasible_next_leg_is_avoided() -> None:
    # Origin expensive; cheap station at mile 50; then nothing until destination at 600.
    # If we stop at cheap@50 with only enough... DP buys at each stop for next leg.
    # From cheap@50 to dest@600 is 550 > 500, so edge infeasible.
    # Must use a station around 200-400 or fail.
    candidates = [
        _c(1, 5.0, 4.00),
        _c(2, 50.0, 1.00),  # trap: cheap but leaves 550 to dest
        _c(3, 300.0, 3.50),  # necessary bridge
    ]
    plan = plan_fuel_stops(candidates, route_distance_miles=600.0)
    opis = {s.opis_id for s in plan.stops}
    assert 3 in opis
    # Path remains feasible
    assert plan.estimated_fuel_cost > 0


def test_expensive_but_necessary_station() -> None:
    candidates = [
        _c(1, 5.0, 2.00),
        _c(2, 400.0, 5.00),  # only bridge to finish a 700-mile route
    ]
    plan = plan_fuel_stops(candidates, route_distance_miles=700.0)
    assert any(s.opis_id == 2 for s in plan.stops)
    assert plan.estimated_fuel_cost >= Decimal("5.00") * (Decimal("300") / Decimal("10"))


def test_two_stations_same_price_deterministic() -> None:
    candidates = [
        _c(1, 5.0, 3.00),
        _c(10, 200.0, 3.00),
        _c(11, 200.0, 3.00),
    ]
    a = plan_fuel_stops(candidates, route_distance_miles=400.0)
    b = plan_fuel_stops(candidates, route_distance_miles=400.0)
    assert [s.opis_id for s in a.stops] == [s.opis_id for s in b.stops]
    assert a.estimated_fuel_cost == b.estimated_fuel_cost


def test_station_exactly_at_range_boundary() -> None:
    candidates = [
        _c(1, 5.0, 3.00),
        _c(2, 500.0, 3.00),  # exactly 500 route miles from mile 0
    ]
    # From origin mile 0 to station at 500: leg = 500 + detours(0) = 500 → feasible
    plan = plan_fuel_stops(candidates, route_distance_miles=500.0)
    assert plan.stops
    # Entire trip 500 from origin may go origin→dest directly without stop 2
    assert plan.total_gallons_required == Decimal("50")


def test_no_feasible_path() -> None:
    candidates = [
        _c(1, 5.0, 3.00),
        # Next station is 600 miles from origin progress — unreachable
        _c(2, 600.0, 3.00),
    ]
    with pytest.raises(RoutePlanningError, match="could not be constructed"):
        plan_fuel_stops(candidates, route_distance_miles=900.0)


def test_no_origin_station() -> None:
    candidates = [_c(1, 80.0, 3.00)]  # outside origin search windows
    with pytest.raises(RoutePlanningError, match="origin fueling station"):
        plan_fuel_stops(candidates, route_distance_miles=200.0)


def test_cost_arithmetic_and_decimal_precision() -> None:
    candidates = [_c(1, 5.0, 3.219)]
    plan = plan_fuel_stops(candidates, route_distance_miles=100.0)
    stop = plan.stops[0]
    assert stop.gallons_to_buy == Decimal("10")
    assert stop.estimated_cost == Decimal("32.19")
    api = stop.to_api_dict()
    assert api["estimated_cost"] == 32.19
    assert api["price_per_gallon"] == 3.219


def test_detour_increases_purchased_gallons() -> None:
    candidates = [_c(1, 5.0, 3.00, detour=5.0)]
    plan = plan_fuel_stops(candidates, route_distance_miles=100.0)
    # baseline 10 gallons; origin detour adds 5 miles → 0.5 gallon
    assert plan.total_gallons_purchased == Decimal("10.5")
    assert plan.total_gallons_required == Decimal("10")


def test_assumptions_document_starting_fuel() -> None:
    plan = plan_fuel_stops([_c(1, 5.0, 3.0)], route_distance_miles=50.0)
    assert "not treated as free" in plan.assumptions["starting_fuel"].lower()
    assert plan.assumptions["origin_station_opis_id"] == 1
