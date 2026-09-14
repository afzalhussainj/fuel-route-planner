from __future__ import annotations

import httpx
import pytest
from apps.fuel.models import FuelStation
from apps.routing.providers.geoapify import GeoapifyProvider
from apps.routing.services.candidates import (
    clear_station_records_cache,
    load_station_records_from_db,
)
from apps.routing.services.planner import LocationInput, RoutePlanningService
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from tests.test_routing_integration import (
    _geocode_payload,
    _mock_transport,
    _route_payload,
    _stations_near_dallas_chicago_route,
)


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    cache.clear()
    clear_station_records_cache()


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.mark.django_db
def test_text_uncached_uses_at_most_three_external_calls(settings) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"
    live = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        live["n"] += 1
        if "geocode/search" in str(request.url):
            text = request.url.params.get("text", "")
            if "Dallas" in text:
                return httpx.Response(200, json=_geocode_payload(32.7767, -96.7970, "Dallas, TX"))
            return httpx.Response(200, json=_geocode_payload(41.8781, -87.6298, "Chicago, IL"))
        return httpx.Response(200, json=_route_payload((32.7767, -96.7970), (41.8781, -87.6298)))

    with httpx.Client(transport=_mock_transport(handler), timeout=5.0) as client:
        result = RoutePlanningService(
            provider=GeoapifyProvider(api_key="test-key", client=client),
            stations=_stations_near_dallas_chicago_route(),
        ).plan(LocationInput(text="Dallas, TX"), LocationInput(text="Chicago, IL"))

    assert live["n"] <= 3
    assert result["meta"]["external_api_calls"] <= 3
    assert result["meta"]["external_api_calls"] == live["n"]


@pytest.mark.django_db
def test_coordinate_uncached_uses_at_most_one_external_call(settings) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"
    live = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        live["n"] += 1
        assert "routing" in str(request.url)
        return httpx.Response(200, json=_route_payload((32.7767, -96.7970), (41.8781, -87.6298)))

    with httpx.Client(transport=_mock_transport(handler), timeout=5.0) as client:
        result = RoutePlanningService(
            provider=GeoapifyProvider(api_key="test-key", client=client),
            stations=_stations_near_dallas_chicago_route(),
        ).plan(
            LocationInput(latitude=32.7767, longitude=-96.7970),
            LocationInput(latitude=41.8781, longitude=-87.6298),
        )

    assert live["n"] <= 1
    assert result["meta"]["external_api_calls"] <= 1


@pytest.mark.django_db
def test_cached_repeat_makes_zero_external_calls(settings) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"
    live = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        live["n"] += 1
        if "geocode/search" in str(request.url):
            text = request.url.params.get("text", "")
            if "Dallas" in text:
                return httpx.Response(200, json=_geocode_payload(32.7767, -96.7970, "Dallas, TX"))
            return httpx.Response(200, json=_geocode_payload(41.8781, -87.6298, "Chicago, IL"))
        return httpx.Response(200, json=_route_payload((32.7767, -96.7970), (41.8781, -87.6298)))

    with httpx.Client(transport=_mock_transport(handler), timeout=5.0) as client:
        first = RoutePlanningService(
            provider=GeoapifyProvider(api_key="test-key", client=client),
            stations=_stations_near_dallas_chicago_route(),
        ).plan(LocationInput(text="Dallas, TX"), LocationInput(text="Chicago, IL"))
        after_first = live["n"]
        second = RoutePlanningService(
            provider=GeoapifyProvider(api_key="test-key", client=client),
            stations=_stations_near_dallas_chicago_route(),
        ).plan(LocationInput(text="Dallas, TX"), LocationInput(text="Chicago, IL"))

    assert after_first <= 3
    assert live["n"] == after_first
    assert first["meta"]["external_api_calls"] == after_first
    assert second["meta"]["external_api_calls"] == 0
    assert second["meta"]["cache"] == {
        "geocode_start": True,
        "geocode_finish": True,
        "route": True,
    }


@pytest.mark.django_db
def test_candidates_and_optimizer_do_not_call_provider(settings) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"
    live = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        live["n"] += 1
        return httpx.Response(200, json=_route_payload((32.7767, -96.7970), (41.8781, -87.6298)))

    with httpx.Client(transport=_mock_transport(handler), timeout=5.0) as client:
        result = RoutePlanningService(
            provider=GeoapifyProvider(api_key="test-key", client=client),
            stations=_stations_near_dallas_chicago_route(),
        ).plan(
            LocationInput(latitude=32.7767, longitude=-96.7970),
            LocationInput(latitude=41.8781, longitude=-87.6298),
        )

    assert live["n"] == 1
    assert result["meta"]["timings_ms"]["candidates"] >= 0
    assert result["meta"]["timings_ms"]["optimizer"] >= 0
    assert result["fuel_plan"]["stops"]


@pytest.mark.django_db
def test_station_load_is_single_query_and_cached() -> None:
    FuelStation.objects.create(
        opis_id=900001,
        name="Query Probe",
        address="1 Main",
        city="Dallas",
        state="TX",
        retail_price="3.100",
        latitude=32.78,
        longitude=-96.80,
        location_accuracy="exact",
    )
    clear_station_records_cache()
    with CaptureQueriesContext(connection) as first:
        rows = load_station_records_from_db(force_refresh=True)
    assert len(first) == 1
    assert rows
    with CaptureQueriesContext(connection) as second:
        again = load_station_records_from_db()
    assert len(second) == 0
    assert again == rows
    assert "rack_id" not in rows[0]
    assert "geocode_confidence" not in rows[0]


@pytest.mark.django_db
def test_phase_timings_present_in_response(settings) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_route_payload((32.7767, -96.7970), (41.8781, -87.6298)))

    with httpx.Client(transport=_mock_transport(handler), timeout=5.0) as client:
        result = RoutePlanningService(
            provider=GeoapifyProvider(api_key="test-key", client=client),
            stations=_stations_near_dallas_chicago_route(),
        ).plan(
            LocationInput(latitude=32.7767, longitude=-96.7970),
            LocationInput(latitude=41.8781, longitude=-87.6298),
        )

    timings = result["meta"]["timings_ms"]
    assert set(timings) == {"geocode", "route", "candidates", "optimizer", "total"}
    assert timings["total"] == result["meta"]["processing_ms"]
    assert timings["total"] >= timings["route"]


@pytest.mark.django_db
def test_httpx_logger_does_not_emit_api_key(settings, caplog) -> None:
    settings.GEOAPIFY_API_KEY = "super-secret-key"
    import logging

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_geocode_payload(32.7767, -96.7970, "Dallas, TX"))

    with caplog.at_level(logging.INFO):
        with httpx.Client(transport=_mock_transport(handler), timeout=5.0) as client:
            GeoapifyProvider(api_key="super-secret-key", client=client).geocode_us("Dallas, TX")

    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "super-secret-key" not in joined
    assert "apiKey=" not in joined


@pytest.mark.django_db
def test_oversized_location_text_rejected(api_client: APIClient) -> None:
    response = api_client.post(
        "/api/v1/routes/plan/",
        data={"start": "A" * 201, "finish": "Chicago, IL"},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
