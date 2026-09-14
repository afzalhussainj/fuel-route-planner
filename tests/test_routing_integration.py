from __future__ import annotations

import json

import httpx
import pytest
from apps.routing.providers.base import GeoPoint
from apps.routing.providers.geoapify import GeoapifyProvider
from apps.routing.services.exceptions import (
    GeocodingError,
    LocationNotFoundError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUpstreamError,
    RoutingError,
)
from apps.routing.services.planner import LocationInput, RoutePlanningService
from django.core.cache import cache
from rest_framework.test import APIClient


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    from apps.routing.services.candidates import clear_station_records_cache

    cache.clear()
    clear_station_records_cache()


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


def _geocode_payload(lat: float, lon: float, label: str, confidence: float = 0.95) -> dict:
    return {
        "results": [
            {
                "lat": lat,
                "lon": lon,
                "country_code": "us",
                "formatted": label,
                "rank": {"confidence": confidence},
            }
        ]
    }


def _route_payload(
    start: tuple[float, float],
    finish: tuple[float, float],
    *,
    distance_m: float = 400000,
    time_s: float = 14000,
) -> dict:
    # GeoJSON [lon, lat]
    return {
        "features": [
            {
                "type": "Feature",
                "properties": {"distance": distance_m, "time": time_s},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [start[1], start[0]],
                        [finish[1], finish[0]],
                    ],
                },
            }
        ]
    }


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def _stations_near_dallas_chicago_route() -> list[dict]:
    """Stations on the mocked Dallas→Chicago segment for fuel planning."""
    return [
        {
            "station_id": 1,
            "opis_id": 1,
            "name": "Origin Fuel Plaza",
            "address": "I-35",
            "city": "Dallas",
            "state": "TX",
            "price_per_gallon": 3.199,
            # ~2% along Dallas→Chicago
            "latitude": 32.7767 + 0.02 * (41.8781 - 32.7767),
            "longitude": -96.7970 + 0.02 * (-87.6298 + 96.7970),
            "location_accuracy": "exact",
        },
        {
            "station_id": 2,
            "opis_id": 2,
            "name": "Midway Stop",
            "address": "I-55",
            "city": "St Louis",
            "state": "MO",
            "price_per_gallon": 3.099,
            "latitude": 32.7767 + 0.50 * (41.8781 - 32.7767),
            "longitude": -96.7970 + 0.50 * (-87.6298 + 96.7970),
            "location_accuracy": "exact",
        },
    ]


@pytest.mark.django_db
def test_valid_us_text_locations(api_client, settings) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"
    calls = {"geocode": 0, "route": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "geocode/search" in url:
            calls["geocode"] += 1
            text = request.url.params.get("text", "")
            if "Dallas" in text:
                return httpx.Response(
                    200, json=_geocode_payload(32.7767, -96.7970, "Dallas, TX, USA")
                )
            return httpx.Response(200, json=_geocode_payload(41.8781, -87.6298, "Chicago, IL, USA"))
        if "routing" in url:
            calls["route"] += 1
            return httpx.Response(
                200,
                json=_route_payload((32.7767, -96.7970), (41.8781, -87.6298)),
            )
        return httpx.Response(404, json={"error": "missing"})

    transport = _mock_transport(handler)
    with httpx.Client(transport=transport, timeout=5.0) as client:
        provider = GeoapifyProvider(api_key="test-key", client=client)
        result = RoutePlanningService(
            provider=provider,
            stations=_stations_near_dallas_chicago_route(),
        ).plan(
            LocationInput(text="Dallas, TX"),
            LocationInput(text="Chicago, IL"),
        )

    assert calls["geocode"] == 2
    assert calls["route"] == 1
    assert result["meta"]["external_api_calls"] == 3
    assert result["route"]["distance_miles"] == pytest.approx(400000 / 1609.344, rel=1e-3)
    assert result["route"]["geometry"]["geometry"]["type"] == "LineString"
    assert result["fuel_plan"]["stops"]
    assert result["fuel_plan"]["stops"][0]["is_origin_fueling"] is True
    assert result["fuel_plan"]["estimated_total_cost_usd"] is not None
    assert result["fuel_plan"]["total_gallons"] == pytest.approx(
        result["route"]["distance_miles"] / 10.0, rel=1e-3
    )
    assert result["vehicle"]["mpg"] == 10
    assert result["meta"]["fuel_planning"] == "cost_optimized"
    assert any("not treated as free" in item.lower() for item in result["assumptions"])


@pytest.mark.django_db
def test_coordinate_inputs_make_one_provider_call(settings) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"
    calls = {"geocode": 0, "route": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "geocode" in url:
            calls["geocode"] += 1
            return httpx.Response(200, json={"results": []})
        calls["route"] += 1
        return httpx.Response(
            200,
            json=_route_payload((32.7767, -96.7970), (41.8781, -87.6298)),
        )

    with httpx.Client(transport=_mock_transport(handler), timeout=5.0) as client:
        provider = GeoapifyProvider(api_key="test-key", client=client)
        result = RoutePlanningService(
            provider=provider,
            stations=_stations_near_dallas_chicago_route(),
        ).plan(
            LocationInput(latitude=32.7767, longitude=-96.7970),
            LocationInput(latitude=41.8781, longitude=-87.6298),
        )

    assert calls["geocode"] == 0
    assert calls["route"] == 1
    assert result["meta"]["external_api_calls"] == 1


@pytest.mark.django_db
def test_text_input_provider_call_count_via_api(api_client, settings, monkeypatch) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocode/search" in str(request.url):
            text = request.url.params.get("text", "")
            if "Dallas" in text:
                return httpx.Response(200, json=_geocode_payload(32.7767, -96.7970, "Dallas, TX"))
            return httpx.Response(200, json=_geocode_payload(41.8781, -87.6298, "Chicago, IL"))
        return httpx.Response(200, json=_route_payload((32.7767, -96.7970), (41.8781, -87.6298)))

    real_provider_cls = GeoapifyProvider

    def factory(*args, **kwargs):
        client = httpx.Client(transport=_mock_transport(handler), timeout=5.0)
        provider = real_provider_cls(api_key="test-key", client=client)
        return provider

    monkeypatch.setattr(
        "apps.routing.services.planner.GeoapifyProvider",
        factory,
    )
    monkeypatch.setattr(
        "apps.routing.services.planner.load_station_records_from_db",
        _stations_near_dallas_chicago_route,
    )
    response = api_client.post(
        "/api/v1/routes/plan/",
        data={"start": "Dallas, TX", "finish": "Chicago, IL"},
        format="json",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["external_api_calls"] == 3
    assert body["fuel_plan"]["stops"]
    assert body["route"]["geometry"]["type"] == "Feature"
    assert body["start"]["input"] == "Dallas, TX"
    assert body["finish"]["input"] == "Chicago, IL"


@pytest.mark.django_db
def test_invalid_location_shape(api_client) -> None:
    response = api_client.post(
        "/api/v1/routes/plan/",
        data={"start": {"address": "Dallas"}, "finish": "Chicago, IL"},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.django_db
def test_location_not_found(settings) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    with httpx.Client(transport=_mock_transport(handler), timeout=5.0) as client:
        provider = GeoapifyProvider(api_key="test-key", client=client)
        with pytest.raises(LocationNotFoundError):
            provider.geocode_us("Nowhereville, ZZ")


@pytest.mark.django_db
def test_non_us_geocoding_result(settings) -> None:
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

    with httpx.Client(transport=_mock_transport(handler), timeout=5.0) as client:
        provider = GeoapifyProvider(api_key="test-key", client=client)
        with pytest.raises(GeocodingError, match="United States"):
            provider.geocode_us("Toronto, ON")


@pytest.mark.django_db
def test_routing_failure(settings) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"features": []})

    with httpx.Client(transport=_mock_transport(handler), timeout=5.0) as client:
        provider = GeoapifyProvider(api_key="test-key", client=client)
        with pytest.raises(RoutingError):
            provider.route(GeoPoint(32.7, -96.8), GeoPoint(41.8, -87.6))


@pytest.mark.django_db
def test_timeout_maps_to_provider_timeout(settings) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"
    settings.HTTP_MAX_RETRIES = 0

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with httpx.Client(transport=_mock_transport(handler), timeout=5.0) as client:
        provider = GeoapifyProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderTimeoutError):
            provider.geocode_us("Dallas, TX")


@pytest.mark.django_db
def test_429_maps_to_rate_limit(settings) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"
    settings.HTTP_MAX_RETRIES = 0

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"})

    with httpx.Client(transport=_mock_transport(handler), timeout=5.0) as client:
        provider = GeoapifyProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderRateLimitError):
            provider.geocode_us("Dallas, TX")


@pytest.mark.django_db
def test_malformed_provider_response(settings) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"
    settings.HTTP_MAX_RETRIES = 0

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json", headers={"content-type": "text/plain"})

    with httpx.Client(transport=_mock_transport(handler), timeout=5.0) as client:
        provider = GeoapifyProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderUpstreamError):
            provider.geocode_us("Dallas, TX")


@pytest.mark.django_db
def test_response_normalization_miles_and_geojson(settings) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_route_payload((32.7767, -96.7970), (41.8781, -87.6298), distance_m=1609.344),
        )

    with httpx.Client(transport=_mock_transport(handler), timeout=5.0) as client:
        provider = GeoapifyProvider(api_key="test-key", client=client)
        result = RoutePlanningService(
            provider=provider,
            stations=_stations_near_dallas_chicago_route(),
        ).plan(
            LocationInput(latitude=32.7767, longitude=-96.7970),
            LocationInput(latitude=41.8781, longitude=-87.6298),
        )

    assert result["route"]["distance_miles"] == 1.0
    geometry = result["route"]["geometry"]["geometry"]
    assert geometry["type"] == "LineString"
    assert geometry["coordinates"][0] == [-96.7970, 32.7767]


@pytest.mark.django_db
def test_cache_hit_makes_zero_external_calls(settings) -> None:
    settings.GEOAPIFY_API_KEY = "test-key"
    live_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        live_calls["n"] += 1
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
        second = RoutePlanningService(
            provider=GeoapifyProvider(api_key="test-key", client=client),
            stations=_stations_near_dallas_chicago_route(),
        ).plan(LocationInput(text="Dallas, TX"), LocationInput(text="Chicago, IL"))

    assert first["meta"]["external_api_calls"] == 3
    assert second["meta"]["external_api_calls"] == 0
    assert second["meta"]["cache"] == {
        "geocode_start": True,
        "geocode_finish": True,
        "route": True,
    }
    assert live_calls["n"] == 3


@pytest.mark.django_db
def test_api_key_never_appears_in_error_payload(api_client, settings, monkeypatch) -> None:
    settings.GEOAPIFY_API_KEY = "super-secret-key"
    settings.HTTP_MAX_RETRIES = 0

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    def factory(*args, **kwargs):
        return GeoapifyProvider(
            api_key="super-secret-key",
            client=httpx.Client(transport=_mock_transport(handler), timeout=5.0),
        )

    monkeypatch.setattr("apps.routing.services.planner.GeoapifyProvider", factory)
    monkeypatch.setattr(
        "apps.routing.services.planner.load_station_records_from_db",
        lambda: [],
    )
    response = api_client.post(
        "/api/v1/routes/plan/",
        data={"start": "Dallas, TX", "finish": "Chicago, IL"},
        format="json",
    )
    assert response.status_code == 502
    body = json.dumps(response.json())
    assert "super-secret-key" not in body
    assert "apiKey" not in body
