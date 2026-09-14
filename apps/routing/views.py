from __future__ import annotations

import httpx
from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.http import require_GET
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.routing.serializers import (
    ErrorResponseSerializer,
    RoutePlanRequestSerializer,
    RoutePlanResponseSerializer,
)
from apps.routing.services.exceptions import FuelRouteError
from apps.routing.services.planner import LocationInput, RoutePlanningService


class RoutePlanView(APIView):
    @extend_schema(
        tags=["Routes"],
        summary="Plan a US driving route with cost-optimized fuel stops",
        description=(
            "Geocode (if needed) and route via Geoapify, then select nearby fuel "
            "stations locally and compute a cost-optimized refueling plan.\n\n"
            "**Call budget:** text inputs ≤ 2 geocode + 1 route; coordinates = 1 route.\n\n"
            "**Vehicle:** 10 MPG, 500-mile max range, 50-gallon usable tank.\n\n"
            "The browser map at `/map/` consumes this endpoint and never calls "
            "Geoapify directly."
        ),
        request=RoutePlanRequestSerializer,
        responses={
            200: OpenApiResponse(
                response=RoutePlanResponseSerializer,
                description="Route and fuel plan resolved successfully.",
            ),
            400: OpenApiResponse(
                response=ErrorResponseSerializer,
                description=(
                    "Validation error, invalid/non-US/unknown location, or ambiguous place."
                ),
            ),
            422: OpenApiResponse(
                response=ErrorResponseSerializer,
                description="No feasible 500-mile-range fuel plan from available stations.",
            ),
            429: OpenApiResponse(
                response=ErrorResponseSerializer,
                description="Upstream provider rate-limited the request.",
            ),
            502: OpenApiResponse(
                response=ErrorResponseSerializer,
                description="Upstream routing/geocoding failure or malformed provider response.",
            ),
            503: OpenApiResponse(
                response=ErrorResponseSerializer,
                description="Service misconfigured (e.g. missing GEOAPIFY_API_KEY).",
            ),
            504: OpenApiResponse(
                response=ErrorResponseSerializer,
                description="Upstream provider timeout.",
            ),
        },
        examples=[
            OpenApiExample(
                "Text locations",
                summary="Place names (≤ 2 geocode + 1 route)",
                value={"start": "Dallas, TX", "finish": "Chicago, IL"},
                request_only=True,
            ),
            OpenApiExample(
                "Coordinates",
                summary="Lat/lon objects (1 route call)",
                value={
                    "start": {"latitude": 32.7767, "longitude": -96.7970},
                    "finish": {"latitude": 41.8781, "longitude": -87.6298},
                },
                request_only=True,
            ),
            OpenApiExample(
                "Successful plan",
                summary="Typical 200 response",
                value={
                    "start": {
                        "input": "Dallas, TX",
                        "latitude": 32.7767,
                        "longitude": -96.797,
                        "label": "Dallas, TX, United States of America",
                    },
                    "finish": {
                        "input": "Chicago, IL",
                        "latitude": 41.8781,
                        "longitude": -87.6298,
                        "label": "Chicago, IL, United States of America",
                    },
                    "route": {
                        "distance_miles": 925.4,
                        "duration_minutes": 820.5,
                        "geometry": {
                            "type": "Feature",
                            "properties": {},
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[-96.797, 32.7767], [-87.6298, 41.8781]],
                            },
                        },
                    },
                    "vehicle": {
                        "mpg": 10,
                        "max_range_miles": 500,
                        "tank_capacity_gallons": 50,
                    },
                    "fuel_plan": {
                        "total_gallons": 92.54,
                        "total_gallons_purchased": 92.54,
                        "estimated_total_cost_usd": 287.15,
                        "stop_count": 3,
                        "stops": [
                            {
                                "station_id": 1,
                                "opis_id": 1001,
                                "name": "Pilot Travel Center",
                                "address": "123 I-35",
                                "city": "Dallas",
                                "state": "TX",
                                "price_per_gallon": 3.199,
                                "latitude": 32.8,
                                "longitude": -96.7,
                                "location_accuracy": "exact",
                                "route_mile": 12.5,
                                "distance_from_route_miles": 0.8,
                                "gallons_to_buy": 48.0,
                                "estimated_cost": 153.55,
                                "is_origin_fueling": True,
                            }
                        ],
                    },
                    "assumptions": [
                        "Vehicle starts empty and must fuel near the origin; fuel is not free."
                    ],
                    "meta": {
                        "external_api_calls": 3,
                        "processing_ms": 180,
                        "timings_ms": {
                            "geocode": 90,
                            "route": 70,
                            "candidates": 15,
                            "optimizer": 2,
                            "total": 180,
                        },
                        "fuel_planning": "cost_optimized",
                        "cache": {
                            "geocode_start": False,
                            "geocode_finish": False,
                            "route": False,
                        },
                        "corridor_miles": 5.0,
                        "corridor_expanded": False,
                        "candidate_station_count": 108,
                        "max_candidate_gap_miles": 120.4,
                        "route_feasible_for_range": True,
                    },
                },
                response_only=True,
                status_codes=["200"],
            ),
            OpenApiExample(
                "Validation failure",
                summary="Missing start/finish or invalid body",
                value={
                    "error": {
                        "code": "validation_error",
                        "message": "start: This field is required.",
                    }
                },
                response_only=True,
                status_codes=["400"],
            ),
            OpenApiExample(
                "Unknown location",
                value={
                    "error": {
                        "code": "location_not_found",
                        "message": "No US location found for 'Nowhereville, ZZ'.",
                    }
                },
                response_only=True,
                status_codes=["400"],
            ),
            OpenApiExample(
                "Non-USA location",
                value={
                    "error": {
                        "code": "invalid_location",
                        "message": "Resolved location is outside the United States.",
                    }
                },
                response_only=True,
                status_codes=["400"],
            ),
            OpenApiExample(
                "Infeasible fuel plan",
                value={
                    "error": {
                        "code": "route_planning_failed",
                        "message": (
                            "No feasible fuel plan within the 500-mile vehicle range "
                            "using available corridor stations."
                        ),
                    }
                },
                response_only=True,
                status_codes=["422"],
            ),
            OpenApiExample(
                "Upstream timeout",
                value={
                    "error": {
                        "code": "upstream_timeout",
                        "message": "Geoapify request timed out.",
                    }
                },
                response_only=True,
                status_codes=["504"],
            ),
            OpenApiExample(
                "No route found",
                value={
                    "error": {
                        "code": "routing_failed",
                        "message": "Routing provider returned no route features.",
                    }
                },
                response_only=True,
                status_codes=["502"],
            ),
        ],
    )
    def post(self, request: Request) -> Response:
        serializer = RoutePlanRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            result = RoutePlanningService().plan(
                start=LocationInput(**data["start"]),
                finish=LocationInput(**data["finish"]),
            )
        except FuelRouteError as exc:
            api_error = exc.to_api_error()
            raise api_error from exc

        return Response(result, status=status.HTTP_200_OK)


def map_view(request):
    return render(request, "routing/map.html")


@require_GET
def map_tile_proxy(request, z: int, x: int, y: int):
    """
    Proxy Geoapify raster tiles so the API key never reaches the browser.

    Tiles are cached briefly in LocMem to keep demo panning light on quota.
    """
    if z < 0 or z > 18 or x < 0 or y < 0:
        return HttpResponseBadRequest("Invalid tile coordinates.")

    api_key = settings.GEOAPIFY_API_KEY
    if not api_key:
        return HttpResponse(
            "Map tiles unavailable: GEOAPIFY_API_KEY is not set.",
            status=503,
            content_type="text/plain",
        )

    cache_key = f"maptile:v1:osm-bright:{z}:{x}:{y}"
    cached = cache.get(cache_key)
    if cached is not None:
        response = HttpResponse(cached, content_type="image/png")
        response["Cache-Control"] = "public, max-age=86400"
        return response

    url = f"https://maps.geoapify.com/v1/tile/osm-bright/{z}/{x}/{y}.png"
    try:
        with httpx.Client(timeout=10.0) as client:
            upstream = client.get(url, params={"apiKey": api_key})
    except httpx.HTTPError:
        return HttpResponse("Upstream tile request failed.", status=502, content_type="text/plain")

    if upstream.status_code != 200:
        return HttpResponse(
            f"Upstream tile error (HTTP {upstream.status_code}).",
            status=502,
            content_type="text/plain",
        )

    body = upstream.content
    cache.set(cache_key, body, timeout=86400)
    response = HttpResponse(body, content_type="image/png")
    response["Cache-Control"] = "public, max-age=86400"
    return response
