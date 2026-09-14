from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.conf import settings

from apps.fuel.services.geocode import (
    GEOAPIFY_BATCH_LIMIT,
    CompositeCityLookup,
    GeoapifyBatchGeocoder,
    GeocodeCache,
    enrich_stations_with_coordinates,
)
from apps.fuel.services.normalize import (
    NormalizedStation,
    normalize_and_dedupe_rows,
)

PROCESSED_FIELDNAMES = [
    "station_id",
    "opis_id",
    "name",
    "address",
    "city",
    "state",
    "rack_id",
    "retail_price",
    "latitude",
    "longitude",
    "location_accuracy",
    "geocode_confidence",
]


def read_source_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    return fieldnames, rows


def stations_to_records(stations: list[NormalizedStation]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for station in stations:
        records.append(
            {
                "station_id": station.opis_id,
                "opis_id": station.opis_id,
                "name": station.name,
                "address": station.address,
                "city": station.city,
                "state": station.state,
                "rack_id": station.rack_id,
                "retail_price": format(station.retail_price, "f"),
                "latitude": None,
                "longitude": None,
                "location_accuracy": None,
                "geocode_confidence": None,
            }
        )
    return records


def write_processed_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PROCESSED_FIELDNAMES)
        writer.writeheader()
        for record in records:
            writer.writerow({key: record.get(key, "") for key in PROCESSED_FIELDNAMES})


def write_stats(path: Path, stats: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stats, indent=2, sort_keys=True), encoding="utf-8")


def prepare_fuel_data(
    *,
    input_path: Path | None = None,
    processed_csv_path: Path | None = None,
    stats_path: Path | None = None,
    geocode_cache_path: Path | None = None,
    force_geocode: bool = False,
    skip_geoapify: bool = False,
    batch_size: int = GEOAPIFY_BATCH_LIMIT,
) -> dict[str, Any]:
    input_path = input_path or Path(settings.FUEL_PRICES_CSV)
    processed_dir = Path(settings.BASE_DIR) / "data" / "processed"
    processed_csv_path = processed_csv_path or (processed_dir / "fuel_stations.csv")
    stats_path = stats_path or (processed_dir / "prepare_stats.json")
    geocode_cache_path = geocode_cache_path or (processed_dir / "geocode_cache.json")

    fieldnames, rows = read_source_csv(input_path)
    stations, import_stats = normalize_and_dedupe_rows(rows, fieldnames=fieldnames)
    records = stations_to_records(stations)

    cache = GeocodeCache(geocode_cache_path)
    geocoder: GeoapifyBatchGeocoder | None = None
    if not skip_geoapify and settings.GEOAPIFY_API_KEY:
        geocoder = GeoapifyBatchGeocoder()

    try:
        geocode_stats = enrich_stations_with_coordinates(
            records,
            cache=cache,
            geocoder=geocoder,
            city_lookup=CompositeCityLookup(),
            force=force_geocode,
            batch_size=batch_size,
        )
    finally:
        if geocoder is not None:
            geocoder.close()

    write_processed_csv(processed_csv_path, records)
    summary = {
        "input_path": str(input_path),
        "processed_csv_path": str(processed_csv_path),
        "geocode_cache_path": str(geocode_cache_path),
        "import": import_stats.to_dict(),
        "geocode": geocode_stats,
        "accuracy_counts": _accuracy_counts(records),
        "duplicate_price_policy": (
            "median Retail Price for identical "
            "OPIS ID + normalized address + city + state "
            "(no timestamps available to choose a newest observation)"
        ),
    }
    write_stats(stats_path, summary)
    return summary


def load_processed_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        records: list[dict[str, Any]] = []
        for row in reader:
            records.append(
                {
                    "station_id": int(row["station_id"]),
                    "opis_id": int(row["opis_id"]),
                    "name": row["name"],
                    "address": row["address"],
                    "city": row["city"],
                    "state": row["state"],
                    "rack_id": int(row["rack_id"]) if row.get("rack_id") else None,
                    "retail_price": Decimal(row["retail_price"]),
                    "latitude": float(row["latitude"]) if row.get("latitude") else None,
                    "longitude": float(row["longitude"]) if row.get("longitude") else None,
                    "location_accuracy": row.get("location_accuracy") or None,
                    "geocode_confidence": (
                        float(row["geocode_confidence"]) if row.get("geocode_confidence") else None
                    ),
                }
            )
    return records


def _accuracy_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        key = str(record.get("location_accuracy") or "missing")
        counts[key] = counts.get(key, 0) + 1
    return counts
