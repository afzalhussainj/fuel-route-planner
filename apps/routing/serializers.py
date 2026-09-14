from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.routing.services.location import normalize_location_text


@extend_schema_field(
    {
        "oneOf": [
            {"type": "string", "example": "Dallas, TX"},
            {
                "type": "object",
                "properties": {
                    "latitude": {"type": "number", "example": 32.7767},
                    "longitude": {"type": "number", "example": -96.7970},
                },
                "required": ["latitude", "longitude"],
                "additionalProperties": False,
            },
        ]
    }
)
class LocationField(serializers.Field):
    """
    Unambiguous location input:

    - text: a JSON string, e.g. \"Dallas, TX\"
    - coordinates: an object with exactly latitude + longitude
    """

    MAX_TEXT_LENGTH = 200

    default_error_messages = {
        "invalid": (
            "Location must be either a string place name or an object with latitude and longitude."
        ),
        "incomplete": "Coordinate locations require both latitude and longitude.",
        "ambiguous": (
            "Coordinate objects must include latitude and longitude only; "
            "use a string for place names."
        ),
        "range": "Latitude must be between -90 and 90; longitude between -180 and 180.",
        "too_long": f"Location text must be at most {MAX_TEXT_LENGTH} characters.",
    }

    def to_internal_value(self, data):
        if isinstance(data, str):
            text = data.strip()
            if not text:
                self.fail("invalid")
            if len(text) > self.MAX_TEXT_LENGTH:
                self.fail("too_long")
            return {
                "text": text,
                "latitude": None,
                "longitude": None,
                "raw_input": text,
            }

        if isinstance(data, dict):
            allowed = {"latitude", "longitude"}
            extra = set(data) - allowed
            if extra:
                self.fail("ambiguous")
            if "latitude" not in data or "longitude" not in data:
                self.fail("incomplete")
            try:
                latitude = float(data["latitude"])
                longitude = float(data["longitude"])
            except (TypeError, ValueError):
                self.fail("invalid")
            if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
                self.fail("range")
            return {
                "text": None,
                "latitude": latitude,
                "longitude": longitude,
                "raw_input": {"latitude": latitude, "longitude": longitude},
            }

        self.fail("invalid")


class RoutePlanRequestSerializer(serializers.Serializer):
    start = LocationField(help_text='US place string, e.g. "Dallas, TX", or coordinates.')
    finish = LocationField(help_text='US place string, e.g. "Chicago, IL", or coordinates.')

    def validate(self, attrs):
        start = attrs["start"]
        finish = attrs["finish"]
        if start.get("text") and finish.get("text"):
            if normalize_location_text(start["text"]) == normalize_location_text(finish["text"]):
                raise serializers.ValidationError("Start and finish must be different locations.")
        if (
            start.get("latitude") is not None
            and finish.get("latitude") is not None
            and abs(start["latitude"] - finish["latitude"]) < 1e-4
            and abs(start["longitude"] - finish["longitude"]) < 1e-4
        ):
            raise serializers.ValidationError("Start and finish must be different locations.")
        return attrs


class ResolvedLocationSerializer(serializers.Serializer):
    input = serializers.JSONField()
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    label = serializers.CharField(required=False, allow_blank=True)


class RouteInfoSerializer(serializers.Serializer):
    distance_miles = serializers.FloatField()
    duration_minutes = serializers.FloatField()
    geometry = serializers.DictField(help_text="GeoJSON Feature (LineString) suitable for Leaflet.")


class VehicleSerializer(serializers.Serializer):
    mpg = serializers.FloatField()
    max_range_miles = serializers.FloatField()
    tank_capacity_gallons = serializers.FloatField()


class FuelStopSerializer(serializers.Serializer):
    station_id = serializers.IntegerField()
    opis_id = serializers.IntegerField()
    name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    price_per_gallon = serializers.FloatField()
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    location_accuracy = serializers.CharField(allow_null=True, required=False)
    route_mile = serializers.FloatField()
    distance_from_route_miles = serializers.FloatField()
    gallons_to_buy = serializers.FloatField()
    estimated_cost = serializers.FloatField()
    is_origin_fueling = serializers.BooleanField()


class FuelPlanSerializer(serializers.Serializer):
    total_gallons = serializers.FloatField()
    total_gallons_purchased = serializers.FloatField()
    estimated_total_cost_usd = serializers.FloatField()
    stop_count = serializers.IntegerField()
    stops = FuelStopSerializer(many=True)


class RoutePlanMetaSerializer(serializers.Serializer):
    external_api_calls = serializers.IntegerField()
    processing_ms = serializers.IntegerField()
    timings_ms = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Phase timings: geocode, route, candidates, optimizer, total.",
    )
    fuel_planning = serializers.CharField()
    cache = serializers.DictField()
    corridor_miles = serializers.FloatField()
    corridor_expanded = serializers.BooleanField()
    candidate_station_count = serializers.IntegerField()
    max_candidate_gap_miles = serializers.FloatField()
    route_feasible_for_range = serializers.BooleanField()


class RoutePlanResponseSerializer(serializers.Serializer):
    start = ResolvedLocationSerializer()
    finish = ResolvedLocationSerializer()
    route = RouteInfoSerializer()
    vehicle = VehicleSerializer()
    fuel_plan = FuelPlanSerializer()
    assumptions = serializers.ListField(child=serializers.CharField())
    meta = RoutePlanMetaSerializer()


class ErrorBodySerializer(serializers.Serializer):
    code = serializers.CharField()
    message = serializers.CharField()


class ErrorResponseSerializer(serializers.Serializer):
    error = ErrorBodySerializer()
