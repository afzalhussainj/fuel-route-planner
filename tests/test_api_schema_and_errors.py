from __future__ import annotations

import httpx
import pytest
from apps.routing.providers.geoapify import GeoapifyProvider
from apps.routing.serializers import RoutePlanResponseSerializer
from apps.routing.services.planner import LocationInput, RoutePlanningService
from django.core.cache import cache
from rest_framework.test import APIClient

from tests.test_routing_integration import (
    _mock_transport,
    _route_payload,
    _stations_near_dallas_chicago_route,
)


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    from apps.routing.services.candidates import clear_station_records_cache

    cache.clear()
    clear_station_records_cache()


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


def _patch_provider(monkeypatch, handler, *, stations=None):
    stations = stations if stations is not None else _stations_near_dallas_chicago_route()

    def factory(*args, **kwargs):
        return GeoapifyProvider(
            api_key="test-key",
            client=httpx.Client(transport=_mock_transport(handler), timeout=5.0),
        )

    monkeypatch.setattr("apps.routing.services.planner.GeoapifyProvider", factory)
    monkeypatch.setattr(
        "apps.routing.services.planner.load_station_records_from_db",
        lambda: stations,
    )


@pytest.mark.django_db
def test_response_matches_public_schema(settings) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_route_payload((32.7767, -96.7970), (41.8781, -87.6298)),
        )

    with httpx.Client(transport=_mock_transport(handler), timeout=5.0) as client:
        result = RoutePlanningService(
            provider=GeoapifyProvider(api_key="test-key", client=client),
            stations=_stations_near_dallas_chicago_route(),
        ).plan(
            LocationInput(
                latitude=32.7767,
                longitude=-96.7970,
                raw_input={"latitude": 32.7767, "longitude": -96.7970},
            ),
            LocationInput(
                latitude=41.8781,
                longitude=-87.6298,
                raw_input={"latitude": 41.8781, "longitude": -87.6298},
            ),
        )

    serializer = RoutePlanResponseSerializer(data=result)
    assert serializer.is_valid(), serializer.errors
    assert set(result) == {
        "start",
        "finish",
        "route",
        "vehicle",
        "fuel_plan",
        "assumptions",
        "meta",
    }
    assert set(result["route"]) == {
        "distance_miles",
        "duration_minutes",
        "geometry",
    }
    assert set(result["fuel_plan"]) >= {
        "total_gallons",
        "estimated_total_cost_usd",
        "stops",
    }
    stop = result["fuel_plan"]["stops"][0]
    assert {
        "name",
        "price_per_gallon",
        "city",
        "state",
        "route_mile",
        "gallons_to_buy",
        "estimated_cost",
    }.issubset(stop)


@pytest.mark.django_db
def test_missing_start(api_client: APIClient) -> None:
    response = api_client.post(
        "/api/v1/routes/plan/",
        data={"finish": "Chicago, IL"},
        format="json",
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert "start" in body["error"]["message"].lower()


@pytest.mark.django_db
def test_missing_finish(api_client: APIClient) -> None:
    response = api_client.post(
        "/api/v1/routes/plan/",
        data={"start": "Dallas, TX"},
        format="json",
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert "finish" in body["error"]["message"].lower()


@pytest.mark.django_db
def test_same_origin_and_destination_text(api_client: APIClient) -> None:
    response = api_client.post(
        "/api/v1/routes/plan/",
        data={"start": "Dallas, TX", "finish": "dallas, tx"},
        format="json",
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert "different" in body["error"]["message"].lower()


@pytest.mark.django_db
def test_same_origin_and_destination_coordinates(api_client: APIClient) -> None:
    response = api_client.post(
        "/api/v1/routes/plan/",
        data={
            "start": {"latitude": 32.7767, "longitude": -96.7970},
            "finish": {"latitude": 32.7767, "longitude": -96.7970},
        },
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.django_db
def test_non_usa_location_via_api(api_client: APIClient, settings, monkeypatch) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "lat": 43.65,
                        "lon": -79.38,
                        "country_code": "ca",
                        "formatted": "Toronto, ON, Canada",
                        "rank": {"confidence": 0.9},
                    }
                ]
            },
        )

    _patch_provider(monkeypatch, handler)
    response = api_client.post(
        "/api/v1/routes/plan/",
        data={"start": "Toronto, ON", "finish": "Chicago, IL"},
        format="json",
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_location"
    assert "united states" in body["error"]["message"].lower()


@pytest.mark.django_db
def test_unknown_location_via_api(api_client: APIClient, settings, monkeypatch) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    _patch_provider(monkeypatch, handler)
    response = api_client.post(
        "/api/v1/routes/plan/",
        data={"start": "Nowhereville, ZZ", "finish": "Chicago, IL"},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "location_not_found"


@pytest.mark.django_db
def test_routing_provider_timeout_via_api(api_client: APIClient, settings, monkeypatch) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"
    settings.HTTP_MAX_RETRIES = 0

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    _patch_provider(monkeypatch, handler)
    response = api_client.post(
        "/api/v1/routes/plan/",
        data={
            "start": {"latitude": 32.7767, "longitude": -96.7970},
            "finish": {"latitude": 41.8781, "longitude": -87.6298},
        },
        format="json",
    )
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "upstream_timeout"


@pytest.mark.django_db
def test_no_route_found_via_api(api_client: APIClient, settings, monkeypatch) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"features": []})

    _patch_provider(monkeypatch, handler)
    response = api_client.post(
        "/api/v1/routes/plan/",
        data={
            "start": {"latitude": 32.7767, "longitude": -96.7970},
            "finish": {"latitude": 41.8781, "longitude": -87.6298},
        },
        format="json",
    )
    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "routing_failed"
    assert "no route" in body["error"]["message"].lower()


@pytest.mark.django_db
def test_infeasible_fuel_plan_via_api(api_client: APIClient, settings, monkeypatch) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"
    long_route_m = 900 * 1609.344

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_route_payload(
                (32.7767, -96.7970),
                (41.8781, -87.6298),
                distance_m=long_route_m,
            ),
        )

    _patch_provider(monkeypatch, handler, stations=[])
    response = api_client.post(
        "/api/v1/routes/plan/",
        data={
            "start": {"latitude": 32.7767, "longitude": -96.7970},
            "finish": {"latitude": 41.8781, "longitude": -87.6298},
        },
        format="json",
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "route_planning_failed"
    assert "error" in body and "message" in body["error"]


@pytest.mark.django_db
def test_map_page_served(api_client: APIClient) -> None:
    response = api_client.get("/map/")
    assert response.status_code == 200
    content = response.content.decode()
    assert "leaflet@1.9.4" in content
    assert "/api/v1/routes/plan/" in content
    assert "never calls Geoapify" in content
    assert "/map/tiles/{z}/{x}/{y}.png" in content
    assert "apiKey" not in content
    assert "tile.openstreetmap.org" not in content


@pytest.mark.django_db
def test_map_tile_proxy_requires_api_key(api_client: APIClient, settings) -> None:
    settings.GEOAPIFY_API_KEY = ""
    response = api_client.get("/map/tiles/4/2/5.png")
    assert response.status_code == 503


@pytest.mark.django_db
def test_map_tile_proxy_fetches_upstream(api_client: APIClient, settings, monkeypatch) -> None:
    settings.GEOAPIFY_API_KEY = "tile-test-key"
    png = b"\x89PNG\r\n\x1a\n" + b"fake-tile"

    class FakeResponse:
        status_code = 200
        content = png

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None):
            assert "maps.geoapify.com" in url
            assert params == {"apiKey": "tile-test-key"}
            assert "apiKey" not in url
            return FakeResponse()

    monkeypatch.setattr("apps.routing.views.httpx.Client", FakeClient)
    first = api_client.get("/map/tiles/4/2/5.png")
    assert first.status_code == 200
    assert first.content == png
    assert first["Content-Type"] == "image/png"
    # Second hit should be served from cache (FakeClient still fine either way).
    second = api_client.get("/map/tiles/4/2/5.png")
    assert second.status_code == 200
    assert second.content == png
