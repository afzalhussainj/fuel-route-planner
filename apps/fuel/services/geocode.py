from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

GEOAPIFY_BATCH_LIMIT = 1000
LOCATION_EXACT = "exact"
LOCATION_APPROXIMATE = "approximate"
LOCATION_CITY = "city"


@dataclass(frozen=True, slots=True)
class GeocodeResult:
    latitude: float
    longitude: float
    location_accuracy: str
    confidence: float | None = None
    provider: str = "geoapify"
    query: str = ""


class CityCoordinateLookup(Protocol):
    def lookup(self, city: str, state: str) -> tuple[float, float] | None: ...


class GeonamesCityLookup:
    """Offline city/state fallback using geonamescache (USA only)."""

    def __init__(self) -> None:
        self._index: dict[tuple[str, str], tuple[float, float]] | None = None

    def _build_index(self) -> dict[tuple[str, str], tuple[float, float]]:
        import geonamescache

        gc = geonamescache.GeonamesCache()
        valid_states = {code.upper() for code in gc.get_us_states()}
        best: dict[tuple[str, str], tuple[int, float, float]] = {}
        for city in gc.get_cities().values():
            if city.get("countrycode") != "US":
                continue
            state = str(city.get("admin1code") or "").upper()
            if state not in valid_states:
                continue
            name = str(city.get("name") or "").strip()
            if not name:
                continue
            try:
                lat = float(city["latitude"])
                lon = float(city["longitude"])
                population = int(city.get("population") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            key = (name.casefold(), state)
            current = best.get(key)
            if current is None or population > current[0]:
                best[key] = (population, lat, lon)
        return {key: (lat, lon) for key, (_, lat, lon) in best.items()}

    def lookup(self, city: str, state: str) -> tuple[float, float] | None:
        if self._index is None:
            self._index = self._build_index()
        return self._index.get((city.strip().casefold(), state.strip().upper()))


class UsCitiesCsvLookup:
    """Secondary local gazetteer for small towns missing from geonamescache."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(settings.US_CITIES_CSV)
        self._index: dict[tuple[str, str], tuple[float, float]] | None = None

    def _build_index(self) -> dict[tuple[str, str], tuple[float, float]]:
        import csv
        import re

        non_alnum = re.compile(r"[^a-z0-9]+")
        index: dict[tuple[str, str], tuple[float, float]] = {}
        if not self.path.is_file():
            return index
        with self.path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                try:
                    state = str(row["STATE_CODE"]).strip().upper()
                    city = non_alnum.sub(" ", str(row["CITY"]).strip().lower()).strip()
                    lat = float(row["LATITUDE"])
                    lon = float(row["LONGITUDE"])
                except (KeyError, TypeError, ValueError):
                    continue
                if city and len(state) == 2:
                    index[(city, state)] = (lat, lon)
        return index

    def lookup(self, city: str, state: str) -> tuple[float, float] | None:
        if self._index is None:
            self._index = self._build_index()
        import re

        non_alnum = re.compile(r"[^a-z0-9]+")
        key = (
            non_alnum.sub(" ", city.strip().lower()).strip(),
            state.strip().upper(),
        )
        return self._index.get(key)


class CompositeCityLookup:
    """Prefer geonamescache, then bundled US cities CSV for small places."""

    def __init__(
        self,
        primary: CityCoordinateLookup | None = None,
        secondary: CityCoordinateLookup | None = None,
    ) -> None:
        self.primary = primary or GeonamesCityLookup()
        self.secondary = secondary or UsCitiesCsvLookup()

    def lookup(self, city: str, state: str) -> tuple[float, float] | None:
        return self.primary.lookup(city, state) or self.secondary.lookup(city, state)


class GeocodeCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict[str, Any]] = {}
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._data = loaded

    def get(self, cache_key: str) -> GeocodeResult | None:
        payload = self._data.get(cache_key)
        if not payload:
            return None
        if payload.get("latitude") is None or payload.get("longitude") is None:
            return None
        return GeocodeResult(
            latitude=float(payload["latitude"]),
            longitude=float(payload["longitude"]),
            location_accuracy=str(payload.get("location_accuracy") or LOCATION_APPROXIMATE),
            confidence=(
                float(payload["confidence"]) if payload.get("confidence") is not None else None
            ),
            provider=str(payload.get("provider") or "cache"),
            query=str(payload.get("query") or ""),
        )

    def set(self, cache_key: str, result: GeocodeResult) -> None:
        self._data[cache_key] = asdict(result)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def __len__(self) -> int:
        return len(self._data)


class GeoapifyBatchGeocoder:
    """Offline bulk geocoder using Geoapify Batch Geocoding API (max 1000/batch)."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        client: httpx.Client | None = None,
        poll_interval_seconds: float = 2.0,
        max_polls: int = 90,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.GEOAPIFY_API_KEY
        self.base_url = (base_url or settings.GEOAPIFY_BASE_URL).rstrip("/")
        self.timeout = timeout if timeout is not None else settings.HTTP_TIMEOUT_SECONDS
        self._client = client
        self._owns_client = client is None
        self.poll_interval_seconds = poll_interval_seconds
        self.max_polls = max_polls

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
            self._owns_client = True
        return self._client

    def geocode_batch(self, queries: list[str]) -> list[GeocodeResult | None]:
        if not self.api_key:
            raise RuntimeError("GEOAPIFY_API_KEY is not configured for batch geocoding.")
        if not queries:
            return []
        if len(queries) > GEOAPIFY_BATCH_LIMIT:
            raise ValueError(f"Geoapify batch limit is {GEOAPIFY_BATCH_LIMIT}; got {len(queries)}")

        response = self.client.post(
            f"{self.base_url}/v1/batch/geocode/search",
            params={
                "apiKey": self.api_key,
                "filter": "countrycode:us",
                "limit": 1,
                "format": "json",
            },
            json=queries,
        )
        if response.status_code not in {200, 202}:
            raise RuntimeError(
                f"Geoapify batch submit failed HTTP {response.status_code}: {response.text[:300]}"
            )

        if response.status_code == 200:
            payload = response.json()
            return _map_batch_payload(payload, queries)

        job_id = response.json().get("id")
        if not job_id:
            raise RuntimeError("Geoapify batch response missing job id.")

        for _ in range(self.max_polls):
            time.sleep(self.poll_interval_seconds)
            poll = self.client.get(
                f"{self.base_url}/v1/batch/geocode/search",
                params={"id": job_id, "apiKey": self.api_key},
            )
            if poll.status_code == 202:
                continue
            if poll.status_code != 200:
                raise RuntimeError(
                    f"Geoapify batch poll failed HTTP {poll.status_code}: {poll.text[:300]}"
                )
            return _map_batch_payload(poll.json(), queries)

        raise TimeoutError(f"Geoapify batch job {job_id} did not complete in time.")


def build_geocode_query(*, name: str, address: str, city: str, state: str) -> str:
    return f"{name}, {address}, {city}, {state}, USA"


def cache_key_for_station(*, opis_id: int, address: str, city: str, state: str) -> str:
    return f"{opis_id}|{address.casefold()}|{city.casefold()}|{state.upper()}"


def classify_geoapify_accuracy(result: dict[str, Any]) -> tuple[str, float | None]:
    rank = result.get("rank") or {}
    confidence = rank.get("confidence")
    try:
        confidence_value = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence_value = None

    result_type = str(result.get("result_type") or "").lower()
    if result_type in {"city", "state", "county", "postcode"}:
        return LOCATION_CITY, confidence_value
    if confidence_value is not None and confidence_value >= 0.7:
        return LOCATION_EXACT, confidence_value
    if confidence_value is not None and confidence_value >= 0.4:
        return LOCATION_APPROXIMATE, confidence_value
    if result_type in {"building", "amenity", "street"}:
        return LOCATION_APPROXIMATE, confidence_value
    return LOCATION_APPROXIMATE, confidence_value


def _map_batch_payload(payload: Any, queries: list[str]) -> list[GeocodeResult | None]:
    if isinstance(payload, dict) and "results" in payload:
        payload = payload["results"]
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected Geoapify batch payload shape.")
    return [
        _parse_geoapify_result(item, query) for item, query in zip(payload, queries, strict=False)
    ]


def _parse_geoapify_result(item: Any, query: str) -> GeocodeResult | None:
    if item is None:
        return None
    if isinstance(item, list):
        item = item[0] if item else None
    if not isinstance(item, dict):
        return None
    if "results" in item and isinstance(item["results"], list):
        item = item["results"][0] if item["results"] else None
        if not isinstance(item, dict):
            return None
    try:
        lat = float(item["lat"])
        lon = float(item["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    country = str(item.get("country_code") or "").lower()
    if country and country != "us":
        return None
    accuracy, confidence = classify_geoapify_accuracy(item)
    return GeocodeResult(
        latitude=lat,
        longitude=lon,
        location_accuracy=accuracy,
        confidence=confidence,
        provider="geoapify",
        query=query,
    )


def enrich_stations_with_coordinates(
    stations: list[dict[str, Any]],
    *,
    cache: GeocodeCache,
    geocoder: GeoapifyBatchGeocoder | None,
    city_lookup: CityCoordinateLookup,
    force: bool = False,
    batch_size: int = GEOAPIFY_BATCH_LIMIT,
) -> dict[str, int]:
    stats = {
        "cache_hits": 0,
        "geoapify_resolved": 0,
        "city_fallback": 0,
        "unresolved": 0,
        "batches_submitted": 0,
    }

    pending: list[int] = []
    for index, station in enumerate(stations):
        key = cache_key_for_station(
            opis_id=int(station["opis_id"]),
            address=str(station["address"]),
            city=str(station["city"]),
            state=str(station["state"]),
        )
        if not force:
            cached = cache.get(key)
            if cached is not None:
                _apply_result(station, cached)
                stats["cache_hits"] += 1
                continue
        pending.append(index)

    if geocoder is not None and pending:
        remaining_after_geo: list[int] = []
        for offset in range(0, len(pending), batch_size):
            chunk = pending[offset : offset + batch_size]
            queries = [
                build_geocode_query(
                    name=str(stations[i]["name"]),
                    address=str(stations[i]["address"]),
                    city=str(stations[i]["city"]),
                    state=str(stations[i]["state"]),
                )
                for i in chunk
            ]
            try:
                results = geocoder.geocode_batch(queries)
                stats["batches_submitted"] += 1
            except Exception:
                logger.exception(
                    "Geoapify batch geocoding failed for %s addresses; "
                    "falling back to city coordinates",
                    len(chunk),
                )
                results = [None] * len(chunk)

            for index, result in zip(chunk, results, strict=False):
                station = stations[index]
                key = cache_key_for_station(
                    opis_id=int(station["opis_id"]),
                    address=str(station["address"]),
                    city=str(station["city"]),
                    state=str(station["state"]),
                )
                if result is None:
                    remaining_after_geo.append(index)
                    continue
                _apply_result(station, result)
                cache.set(key, result)
                stats["geoapify_resolved"] += 1
        pending = remaining_after_geo

    for index in pending:
        station = stations[index]
        point = city_lookup.lookup(str(station["city"]), str(station["state"]))
        key = cache_key_for_station(
            opis_id=int(station["opis_id"]),
            address=str(station["address"]),
            city=str(station["city"]),
            state=str(station["state"]),
        )
        if point is None:
            stats["unresolved"] += 1
            continue
        result = GeocodeResult(
            latitude=point[0],
            longitude=point[1],
            location_accuracy=LOCATION_CITY,
            confidence=None,
            provider="geonamescache",
            query=f"{station['city']}, {station['state']}",
        )
        _apply_result(station, result)
        cache.set(key, result)
        stats["city_fallback"] += 1

    cache.save()
    return stats


def _apply_result(station: dict[str, Any], result: GeocodeResult) -> None:
    station["latitude"] = result.latitude
    station["longitude"] = result.longitude
    station["location_accuracy"] = result.location_accuracy
    station["geocode_confidence"] = result.confidence
