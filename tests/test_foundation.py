from __future__ import annotations

import pytest
from django.conf import settings
from django.urls import reverse
from rest_framework.test import APIClient


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


def test_health_endpoint(api_client: APIClient) -> None:
    response = api_client.get("/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_url_name_resolves() -> None:
    assert reverse("health") == "/health/"


def test_route_plan_url_is_wired(api_client: APIClient, settings) -> None:
    settings.GEOAPIFY_API_KEY = ""
    response = api_client.post(
        "/api/v1/routes/plan/",
        data={"start": "Chicago, IL", "finish": "Dallas, TX"},
        format="json",
    )
    # Without a configured provider key the endpoint fails explicitly.
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "service_unavailable"


def test_route_plan_validation_error_shape(api_client: APIClient) -> None:
    response = api_client.post("/api/v1/routes/plan/", data={}, format="json")
    assert response.status_code == 400
    body = response.json()
    assert "error" in body
    assert body["error"]["code"] == "validation_error"
    assert "message" in body["error"]


def test_openapi_schema_endpoint(api_client: APIClient) -> None:
    response = api_client.get("/api/schema/")
    assert response.status_code == 200
    assert "openapi" in response.data
    assert response.data["info"]["title"] == "Fuel Route API"


def test_swagger_docs_page(api_client: APIClient) -> None:
    response = api_client.get("/api/docs/")
    assert response.status_code == 200


def test_settings_import_geoapify_and_timeout() -> None:
    assert hasattr(settings, "GEOAPIFY_API_KEY")
    assert hasattr(settings, "HTTP_TIMEOUT_SECONDS")
    assert hasattr(settings, "HTTP_CONNECT_TIMEOUT_SECONDS")
    assert hasattr(settings, "HTTP_READ_TIMEOUT_SECONDS")
    assert hasattr(settings, "CACHES")
    assert settings.HTTP_TIMEOUT_SECONDS > 0
    assert settings.GEOAPIFY_BASE_URL.startswith("http")
