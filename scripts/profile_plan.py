#!/usr/bin/env python
"""Measure local planning phase timings with a mocked Geoapify provider."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import django
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from apps.routing.providers.geoapify import GeoapifyProvider  # noqa: E402
from apps.routing.services.candidates import (  # noqa: E402
    clear_station_records_cache,
    load_station_records_from_db,
)
from apps.routing.services.planner import LocationInput, RoutePlanningService  # noqa: E402
from django.core.cache import cache  # noqa: E402


def _route_payload(start: tuple[float, float], finish: tuple[float, float]) -> dict:
    return {
        "features": [
            {
                "type": "Feature",
                "properties": {"distance": 400000, "time": 14000},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [start[1], start[0]],
                        [-94.0, 35.0],
                        [-90.2, 38.6],
                        [finish[1], finish[0]],
                    ],
                },
            }
        ]
    }


def main() -> int:
    cache.clear()
    clear_station_records_cache()
    stations = load_station_records_from_db(force_refresh=True)
    if not stations:
        print("No stations in DB. Run: uv run python manage.py import_fuel_stations")
        return 1

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_route_payload((32.7767, -96.7970), (41.8781, -87.6298)),
        )

    with httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0) as client:
        first = RoutePlanningService(
            provider=GeoapifyProvider(api_key="profile-key", client=client),
            stations=stations,
        ).plan(
            LocationInput(latitude=32.7767, longitude=-96.7970),
            LocationInput(latitude=41.8781, longitude=-87.6298),
        )
        second = RoutePlanningService(
            provider=GeoapifyProvider(api_key="profile-key", client=client),
            stations=stations,
        ).plan(
            LocationInput(latitude=32.7767, longitude=-96.7970),
            LocationInput(latitude=41.8781, longitude=-87.6298),
        )

    print(f"stations_loaded={len(stations)}")
    print(f"first_external_api_calls={first['meta']['external_api_calls']}")
    print(f"first_timings_ms={first['meta']['timings_ms']}")
    print(f"first_candidates={first['meta']['candidate_station_count']}")
    print(f"second_external_api_calls={second['meta']['external_api_calls']}")
    print(f"second_timings_ms={second['meta']['timings_ms']}")
    print(f"second_cache={second['meta']['cache']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
