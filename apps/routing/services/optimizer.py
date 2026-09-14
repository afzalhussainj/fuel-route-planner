from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from apps.routing.services.candidates import StationCandidate
from apps.routing.services.exceptions import RoutePlanningError
from apps.routing.services.vehicle import (
    ORIGIN_SEARCH_MAX_MILES,
    ORIGIN_SEARCH_MILES,
    gallons_for_miles,
    tank_capacity_gallons,
    vehicle_max_range_miles,
    vehicle_mpg,
)

MONEY_QUANT = Decimal("0.01")
GALLON_QUANT = Decimal("0.001")
MILE_QUANT = Decimal("0.001")


@dataclass(frozen=True, slots=True)
class FuelPlanStop:
    station_id: int
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    price_per_gallon: Decimal
    latitude: float
    longitude: float
    location_accuracy: str | None
    route_mile: Decimal
    distance_from_route_miles: Decimal
    gallons_to_buy: Decimal
    estimated_cost: Decimal
    is_origin_fueling: bool

    def to_api_dict(self) -> dict:
        return {
            "station_id": self.station_id,
            "opis_id": self.opis_id,
            "name": self.name,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "price_per_gallon": float(
                self.price_per_gallon.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
            ),
            "latitude": self.latitude,
            "longitude": self.longitude,
            "location_accuracy": self.location_accuracy,
            "route_mile": float(self.route_mile.quantize(MILE_QUANT, rounding=ROUND_HALF_UP)),
            "distance_from_route_miles": float(
                self.distance_from_route_miles.quantize(MILE_QUANT, rounding=ROUND_HALF_UP)
            ),
            "gallons_to_buy": float(
                self.gallons_to_buy.quantize(GALLON_QUANT, rounding=ROUND_HALF_UP)
            ),
            "estimated_cost": float(
                self.estimated_cost.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
            ),
            "is_origin_fueling": self.is_origin_fueling,
        }


@dataclass(frozen=True, slots=True)
class FuelPlan:
    stops: list[FuelPlanStop]
    route_distance_miles: Decimal
    total_gallons_required: Decimal
    total_gallons_purchased: Decimal
    estimated_fuel_cost: Decimal
    mpg: Decimal
    max_range_miles: Decimal
    tank_capacity_gallons: Decimal
    assumptions: dict

    def to_api_summary(self) -> dict:
        return {
            "mpg": float(self.mpg),
            "max_range_miles": float(self.max_range_miles),
            "tank_capacity_gallons": float(
                self.tank_capacity_gallons.quantize(GALLON_QUANT, rounding=ROUND_HALF_UP)
            ),
            "total_gallons_required": float(
                self.total_gallons_required.quantize(GALLON_QUANT, rounding=ROUND_HALF_UP)
            ),
            "total_gallons_purchased": float(
                self.total_gallons_purchased.quantize(GALLON_QUANT, rounding=ROUND_HALF_UP)
            ),
            "fuel_stop_count": len(self.stops),
            "estimated_fuel_cost_usd": float(
                self.estimated_fuel_cost.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
            ),
            "assumptions": self.assumptions,
        }


@dataclass(frozen=True, slots=True)
class _Node:
    index: int
    mile_marker: Decimal
    distance_from_route: Decimal
    price_per_gallon: Decimal | None
    candidate: StationCandidate | None
    is_origin: bool
    is_destination: bool

    @property
    def can_fuel(self) -> bool:
        return self.price_per_gallon is not None and not self.is_destination


def plan_fuel_stops(
    candidates: Sequence[StationCandidate],
    *,
    route_distance_miles: float | Decimal,
    origin_search_miles: Decimal | None = None,
    origin_search_max_miles: Decimal | None = None,
) -> FuelPlan:
    """
    Cost-optimized refueling plan for a fixed route and corridor candidates.

    Graph/DP model:
    - virtual destination node at route end
    - origin fueling node from the cheapest station in an early-route window
    - en-route candidate stations
    - edge i→j is feasible only if effective forward distance ≤ vehicle range
    - fuel for a leg is purchased at node i's price

    This is optimal for the modeled graph (fixed route, candidate set, detour
    rule, and origin assumption), not a claim of global real-world optimality.
    """
    route_miles = Decimal(str(route_distance_miles))
    if route_miles < 0:
        raise RoutePlanningError("Route distance must be non-negative.")

    mpg = vehicle_mpg()
    max_range = vehicle_max_range_miles()
    tank = tank_capacity_gallons()
    search = origin_search_miles if origin_search_miles is not None else ORIGIN_SEARCH_MILES
    search_max = (
        origin_search_max_miles if origin_search_max_miles is not None else ORIGIN_SEARCH_MAX_MILES
    )

    origin_candidate, origin_window = _select_origin_station(
        candidates, search_miles=search, search_max_miles=search_max
    )

    nodes = _build_nodes(
        origin_candidate=origin_candidate,
        candidates=candidates,
        route_miles=route_miles,
    )
    path_edges = _shortest_fuel_path(nodes, max_range=max_range, mpg=mpg)

    stops: list[FuelPlanStop] = []
    purchased = Decimal("0")
    cost_total = Decimal("0")
    for frm, _to, _leg_miles, gallons, cost in path_edges:
        if not frm.can_fuel or frm.candidate is None:
            continue
        purchased += gallons
        cost_total += cost
        stops.append(
            FuelPlanStop(
                station_id=frm.candidate.station_id,
                opis_id=frm.candidate.opis_id,
                name=frm.candidate.name,
                address=frm.candidate.address,
                city=frm.candidate.city,
                state=frm.candidate.state,
                price_per_gallon=Decimal(str(frm.candidate.price_per_gallon)),
                latitude=frm.candidate.latitude,
                longitude=frm.candidate.longitude,
                location_accuracy=frm.candidate.location_accuracy,
                route_mile=frm.mile_marker,
                distance_from_route_miles=frm.distance_from_route,
                gallons_to_buy=gallons,
                estimated_cost=cost,
                is_origin_fueling=frm.is_origin,
            )
        )

    baseline_gallons = gallons_for_miles(route_miles)
    assumptions = {
        "starting_fuel": (
            "Vehicle is fueled at the origin before departure at the best eligible "
            "station near the start of the route; initial fuel is not treated as free."
        ),
        "origin_station_opis_id": origin_candidate.opis_id,
        "origin_search_miles": float(origin_window),
        "detour_model": (
            "Origin adds 1× distance_from_route; each en-route stop adds "
            "2× distance_from_route to the inbound leg; destination adds none."
        ),
        "algorithm": (
            "Minimum-cost path over origin → candidates → destination with "
            "edges limited to the vehicle max range."
        ),
        "vehicle_mpg": float(mpg),
        "vehicle_max_range_miles": float(max_range),
        "tank_capacity_gallons": float(tank),
    }

    return FuelPlan(
        stops=stops,
        route_distance_miles=route_miles,
        total_gallons_required=baseline_gallons,
        total_gallons_purchased=purchased,
        estimated_fuel_cost=cost_total,
        mpg=mpg,
        max_range_miles=max_range,
        tank_capacity_gallons=tank,
        assumptions=assumptions,
    )


def _select_origin_station(
    candidates: Sequence[StationCandidate],
    *,
    search_miles: Decimal,
    search_max_miles: Decimal,
) -> tuple[StationCandidate, Decimal]:
    for window in (search_miles, search_max_miles):
        eligible = [
            c for c in candidates if Decimal(str(c.mile_marker)) <= window + Decimal("1e-9")
        ]
        if eligible:
            best = min(
                eligible,
                key=lambda c: (
                    Decimal(str(c.price_per_gallon)),
                    Decimal(str(c.distance_from_route_miles)),
                    Decimal(str(c.mile_marker)),
                    c.opis_id,
                ),
            )
            return best, window
    raise RoutePlanningError(
        "Could not determine an origin fueling station near the start of the route "
        f"within {float(search_max_miles):.0f} miles of route progress. "
        "A 500-mile-range fuel plan requires a priced station for the initial fill."
    )


def _build_nodes(
    *,
    origin_candidate: StationCandidate,
    candidates: Sequence[StationCandidate],
    route_miles: Decimal,
) -> list[_Node]:
    nodes: list[_Node] = [
        _Node(
            index=0,
            mile_marker=Decimal("0"),
            distance_from_route=Decimal(str(origin_candidate.distance_from_route_miles)),
            price_per_gallon=Decimal(str(origin_candidate.price_per_gallon)),
            candidate=origin_candidate,
            is_origin=True,
            is_destination=False,
        )
    ]
    for candidate in sorted(candidates, key=lambda c: (c.mile_marker, c.opis_id)):
        if candidate.opis_id == origin_candidate.opis_id:
            continue
        mile = Decimal(str(candidate.mile_marker))
        if mile <= 0 or mile >= route_miles:
            continue
        nodes.append(
            _Node(
                index=len(nodes),
                mile_marker=mile,
                distance_from_route=Decimal(str(candidate.distance_from_route_miles)),
                price_per_gallon=Decimal(str(candidate.price_per_gallon)),
                candidate=candidate,
                is_origin=False,
                is_destination=False,
            )
        )
    nodes.append(
        _Node(
            index=len(nodes),
            mile_marker=route_miles,
            distance_from_route=Decimal("0"),
            price_per_gallon=None,
            candidate=None,
            is_origin=False,
            is_destination=True,
        )
    )
    # Re-index after construction
    return [
        _Node(
            index=i,
            mile_marker=node.mile_marker,
            distance_from_route=node.distance_from_route,
            price_per_gallon=node.price_per_gallon,
            candidate=node.candidate,
            is_origin=node.is_origin,
            is_destination=node.is_destination,
        )
        for i, node in enumerate(nodes)
    ]


def _leg_miles(frm: _Node, to: _Node) -> Decimal:
    base = to.mile_marker - frm.mile_marker
    if base < 0:
        return Decimal("Infinity")
    extra = Decimal("0")
    if frm.is_origin:
        extra += frm.distance_from_route
    if not to.is_destination:
        extra += to.distance_from_route * 2
    return base + extra


def _shortest_fuel_path(
    nodes: list[_Node],
    *,
    max_range: Decimal,
    mpg: Decimal,
) -> list[tuple[_Node, _Node, Decimal, Decimal, Decimal]]:
    n = len(nodes)
    inf = Decimal("Infinity")
    dp = [inf] * n
    parent: list[int | None] = [None] * n
    dp[0] = Decimal("0")

    for j in range(1, n):
        for i in range(0, j):
            if not nodes[i].can_fuel:
                continue
            dist = _leg_miles(nodes[i], nodes[j])
            if dist > max_range + Decimal("1e-9"):
                continue
            gallons = dist / mpg
            price = nodes[i].price_per_gallon
            assert price is not None
            cost = dp[i] + gallons * price
            if cost < dp[j]:
                dp[j] = cost
                parent[j] = i

    if dp[-1] == inf or (n > 1 and parent[-1] is None):
        raise RoutePlanningError(
            "A 500-mile-range fuel plan could not be constructed from the available "
            "station dataset for this route. No feasible sequence of refueling stops "
            "keeps every leg within the vehicle's maximum range."
        )

    # Reconstruct edges destination ← ... ← origin
    edges_rev: list[tuple[int, int]] = []
    cur = n - 1
    while cur != 0:
        prev = parent[cur]
        if prev is None:
            raise RoutePlanningError(
                "A 500-mile-range fuel plan could not be constructed from the available "
                "station dataset for this route."
            )
        edges_rev.append((prev, cur))
        cur = prev
    edges_rev.reverse()

    result: list[tuple[_Node, _Node, Decimal, Decimal, Decimal]] = []
    for i, j in edges_rev:
        dist = _leg_miles(nodes[i], nodes[j])
        gallons = dist / mpg
        price = nodes[i].price_per_gallon
        assert price is not None
        result.append((nodes[i], nodes[j], dist, gallons, gallons * price))
    return result
