from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from apps.fuel.models import FuelStation
from apps.fuel.services.geocode import (
    LOCATION_APPROXIMATE,
    LOCATION_CITY,
    LOCATION_EXACT,
    GeoapifyBatchGeocoder,
    GeocodeCache,
    GeocodeResult,
    classify_geoapify_accuracy,
    enrich_stations_with_coordinates,
)
from apps.fuel.services.normalize import (
    FuelDataError,
    RawStationRow,
    aggregate_station_group,
    normalize_and_dedupe_rows,
    normalize_whitespace,
    validate_columns,
)
from django.core.management import call_command

REQUIRED = [
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
]


def test_required_column_validation() -> None:
    missing = validate_columns(["OPIS Truckstop ID", "City"])
    assert "Truckstop Name" in missing
    assert "Retail Price" in missing


def test_missing_columns_raise() -> None:
    with pytest.raises(FuelDataError):
        normalize_and_dedupe_rows([], fieldnames=["City"])


def test_whitespace_normalization() -> None:
    assert normalize_whitespace("  Pilot   Travel   Center  ") == "Pilot Travel Center"


def test_us_filtering_removes_canadian_rows() -> None:
    rows = [
        {
            "OPIS Truckstop ID": "1",
            "Truckstop Name": "US Stop",
            "Address": "I-90",
            "City": "Chicago",
            "State": "IL",
            "Rack ID": "1",
            "Retail Price": "3.10",
        },
        {
            "OPIS Truckstop ID": "2",
            "Truckstop Name": "CA Stop",
            "Address": "Hwy 1",
            "City": "Calgary",
            "State": "AB",
            "Rack ID": "2",
            "Retail Price": "3.20",
        },
        {
            "OPIS Truckstop ID": "3",
            "Truckstop Name": "ON Stop",
            "Address": "401",
            "City": "Toronto",
            "State": "ON",
            "Rack ID": "3",
            "Retail Price": "3.30",
        },
    ]
    stations, stats = normalize_and_dedupe_rows(rows, fieldnames=REQUIRED)
    assert stats.source_rows == 3
    assert stats.us_rows == 1
    assert stats.canadian_or_non_us_rows == 2
    assert len(stations) == 1
    assert stations[0].state == "IL"


def test_invalid_price_handling() -> None:
    rows = [
        {
            "OPIS Truckstop ID": "1",
            "Truckstop Name": "Bad",
            "Address": "I-10",
            "City": "Houston",
            "State": "TX",
            "Rack ID": "1",
            "Retail Price": "not-a-price",
        },
        {
            "OPIS Truckstop ID": "2",
            "Truckstop Name": "Neg",
            "Address": "I-10",
            "City": "Houston",
            "State": "TX",
            "Rack ID": "1",
            "Retail Price": "-1.00",
        },
    ]
    stations, stats = normalize_and_dedupe_rows(rows, fieldnames=REQUIRED)
    assert stations == []
    assert stats.malformed_prices == 2


def test_median_price_duplicate_aggregation() -> None:
    rows = [
        RawStationRow(
            opis_id=10,
            name="PILOT #1",
            address="I-80 EXIT 1",
            city="Omaha",
            state="NE",
            rack_id=1,
            retail_price=Decimal("3.10"),
            source_row_number=2,
        ),
        RawStationRow(
            opis_id=10,
            name="PILOT TRAVEL CENTER #1",
            address="I-80 EXIT 1",
            city="Omaha",
            state="NE",
            rack_id=1,
            retail_price=Decimal("3.50"),
            source_row_number=3,
        ),
        RawStationRow(
            opis_id=10,
            name="PILOT #1",
            address="I-80 EXIT 1",
            city="Omaha",
            state="NE",
            rack_id=1,
            retail_price=Decimal("3.30"),
            source_row_number=4,
        ),
    ]
    station = aggregate_station_group(rows)
    assert station.retail_price == Decimal("3.30")
    assert station.name == "PILOT TRAVEL CENTER #1"


def test_duplicate_aggregation_end_to_end() -> None:
    rows = [
        {
            "OPIS Truckstop ID": "10",
            "Truckstop Name": "A",
            "Address": "I-80",
            "City": "Omaha",
            "State": "ne",
            "Rack ID": "1",
            "Retail Price": "3.10",
        },
        {
            "OPIS Truckstop ID": "10",
            "Truckstop Name": "A Longer Name",
            "Address": " I-80 ",
            "City": " Omaha ",
            "State": "NE",
            "Rack ID": "1",
            "Retail Price": "3.50",
        },
        {
            "OPIS Truckstop ID": "10",
            "Truckstop Name": "A",
            "Address": "I-80",
            "City": "Omaha",
            "State": "NE",
            "Rack ID": "1",
            "Retail Price": "3.30",
        },
    ]
    stations, stats = normalize_and_dedupe_rows(rows, fieldnames=REQUIRED)
    assert stats.unique_stations == 1
    assert stats.duplicate_groups == 1
    assert stats.groups_with_price_variance == 1
    assert stations[0].retail_price == Decimal("3.30")
    assert stations[0].name == "A Longer Name"


def test_classify_exact_versus_approximate() -> None:
    exact, conf = classify_geoapify_accuracy(
        {"result_type": "building", "rank": {"confidence": 0.92}}
    )
    approx, _ = classify_geoapify_accuracy({"result_type": "street", "rank": {"confidence": 0.55}})
    city, _ = classify_geoapify_accuracy({"result_type": "city", "rank": {"confidence": 0.99}})
    assert exact == LOCATION_EXACT
    assert conf == pytest.approx(0.92)
    assert approx == LOCATION_APPROXIMATE
    assert city == LOCATION_CITY


def test_already_cached_coordinates_are_not_rerequested(tmp_path: Path) -> None:
    cache = GeocodeCache(tmp_path / "cache.json")
    cache.set(
        "7|i-44 exit|big cabin|OK",
        GeocodeResult(
            latitude=36.5,
            longitude=-95.2,
            location_accuracy=LOCATION_EXACT,
            confidence=0.9,
            provider="geoapify",
            query="cached",
        ),
    )
    stations = [
        {
            "opis_id": 7,
            "name": "Woodshed",
            "address": "I-44 EXIT",
            "city": "Big Cabin",
            "state": "OK",
        }
    ]

    class FailingGeocoder:
        def geocode_batch(self, queries):  # pragma: no cover - must not be called
            raise AssertionError("geocoder should not be called for cache hits")

    class FailingCities:
        def lookup(self, city, state):  # pragma: no cover
            raise AssertionError("city lookup should not be called for cache hits")

    stats = enrich_stations_with_coordinates(
        stations,
        cache=cache,
        geocoder=FailingGeocoder(),  # type: ignore[arg-type]
        city_lookup=FailingCities(),
    )
    assert stats["cache_hits"] == 1
    assert stations[0]["latitude"] == 36.5
    assert stations[0]["location_accuracy"] == LOCATION_EXACT


def test_geocoder_failure_falls_back_to_city(tmp_path: Path) -> None:
    cache = GeocodeCache(tmp_path / "cache.json")
    stations = [
        {
            "opis_id": 1,
            "name": "Stop",
            "address": "I-10 EXIT 1",
            "city": "Houston",
            "state": "TX",
        }
    ]

    class BoomGeocoder:
        def geocode_batch(self, queries):
            raise RuntimeError("provider down")

    class CityLookup:
        def lookup(self, city, state):
            assert city == "Houston"
            assert state == "TX"
            return 29.76, -95.37

    stats = enrich_stations_with_coordinates(
        stations,
        cache=cache,
        geocoder=BoomGeocoder(),  # type: ignore[arg-type]
        city_lookup=CityLookup(),
    )
    assert stats["city_fallback"] == 1
    assert stations[0]["location_accuracy"] == LOCATION_CITY
    assert stations[0]["latitude"] == pytest.approx(29.76)


def test_geoapify_batch_parser_with_mock_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "batch/geocode/search" in str(request.url)
        return httpx.Response(
            200,
            json=[
                {
                    "lat": 41.87,
                    "lon": -87.62,
                    "country_code": "us",
                    "result_type": "building",
                    "rank": {"confidence": 0.88},
                }
            ],
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        geocoder = GeoapifyBatchGeocoder(api_key="test", client=client)
        results = geocoder.geocode_batch(["Chicago, IL, USA"])
    assert results[0] is not None
    assert results[0].location_accuracy == LOCATION_EXACT


@pytest.mark.django_db
def test_idempotent_import(tmp_path: Path) -> None:
    processed = tmp_path / "fuel_stations.csv"
    processed.write_text(
        "station_id,opis_id,name,address,city,state,rack_id,retail_price,"
        "latitude,longitude,location_accuracy,geocode_confidence\n"
        "7,7,Woodshed,I-44,Big Cabin,OK,307,3.100000,36.5,-95.2,city,\n",
        encoding="utf-8",
    )
    call_command("import_fuel_stations", input=str(processed))
    call_command("import_fuel_stations", input=str(processed))
    assert FuelStation.objects.count() == 1
    station = FuelStation.objects.get(opis_id=7)
    assert station.location_accuracy == LOCATION_CITY
    assert station.retail_price == Decimal("3.100000")
