from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

US_STATE_CODES = frozenset(
    {
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "DC",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
    }
)

REQUIRED_COLUMNS = (
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
)

_WHITESPACE_RE = re.compile(r"\s+")


class FuelDataError(ValueError):
    """Raised when the source CSV cannot be validated."""


@dataclass(frozen=True, slots=True)
class RawStationRow:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    rack_id: int | None
    retail_price: Decimal
    source_row_number: int


@dataclass(frozen=True, slots=True)
class NormalizedStation:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    rack_id: int | None
    retail_price: Decimal


@dataclass
class ImportStats:
    source_rows: int = 0
    missing_required_columns: list[str] = field(default_factory=list)
    malformed_prices: int = 0
    malformed_opis_ids: int = 0
    canadian_or_non_us_rows: int = 0
    us_rows: int = 0
    unique_stations: int = 0
    duplicate_groups: int = 0
    groups_with_price_variance: int = 0
    rejected_rows: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_whitespace(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value.strip())


def normalize_state(value: str) -> str:
    return normalize_whitespace(value).upper()


def normalize_name(value: str) -> str:
    return normalize_whitespace(value)


def normalize_city(value: str) -> str:
    return normalize_whitespace(value)


def normalize_address(value: str) -> str:
    return normalize_whitespace(value)


def parse_price(value: object) -> Decimal:
    text = normalize_whitespace(str(value))
    if not text:
        raise InvalidOperation("empty price")
    price = Decimal(text)
    if price < 0:
        raise InvalidOperation("negative price")
    return price


def parse_opis_id(value: object) -> int:
    text = normalize_whitespace(str(value))
    if not text:
        raise ValueError("empty opis id")
    return int(text)


def parse_rack_id(value: object) -> int | None:
    text = normalize_whitespace(str(value or ""))
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def validate_columns(fieldnames: Iterable[str] | None) -> list[str]:
    if fieldnames is None:
        return list(REQUIRED_COLUMNS)
    present = set(fieldnames)
    return [column for column in REQUIRED_COLUMNS if column not in present]


def choose_canonical_name(names: list[str]) -> str:
    """Prefer the longest normalized name; break ties lexicographically."""
    return sorted(names, key=lambda name: (-len(name), name.casefold(), name))[0]


def aggregate_station_group(rows: list[RawStationRow]) -> NormalizedStation:
    prices = [row.retail_price for row in rows]
    names = [row.name for row in rows if row.name]
    rack_ids = [row.rack_id for row in rows if row.rack_id is not None]
    first = rows[0]
    return NormalizedStation(
        opis_id=first.opis_id,
        name=choose_canonical_name(names) if names else first.name,
        address=first.address,
        city=first.city,
        state=first.state,
        rack_id=rack_ids[0] if rack_ids else None,
        # Median is used because repeated OPIS/address rows carry no timestamp
        # indicating which retail observation is newest.
        retail_price=Decimal(str(median(prices))),
    )


def normalize_and_dedupe_rows(
    rows: Iterable[dict[str, str]],
    *,
    fieldnames: Iterable[str] | None,
) -> tuple[list[NormalizedStation], ImportStats]:
    stats = ImportStats()
    missing = validate_columns(fieldnames)
    stats.missing_required_columns = missing
    if missing:
        raise FuelDataError(f"CSV missing required columns: {', '.join(missing)}")

    grouped: dict[tuple[int, str, str, str], list[RawStationRow]] = {}
    for index, row in enumerate(rows, start=2):
        stats.source_rows += 1

        try:
            opis_id = parse_opis_id(row.get("OPIS Truckstop ID", ""))
        except (ValueError, TypeError):
            stats.malformed_opis_ids += 1
            stats.rejected_rows += 1
            continue

        try:
            retail_price = parse_price(row.get("Retail Price", ""))
        except (InvalidOperation, TypeError, ValueError):
            stats.malformed_prices += 1
            stats.rejected_rows += 1
            continue

        state = normalize_state(str(row.get("State", "")))
        if not is_us_state(state):
            stats.canadian_or_non_us_rows += 1
            stats.rejected_rows += 1
            continue

        normalized = RawStationRow(
            opis_id=opis_id,
            name=normalize_name(str(row.get("Truckstop Name", ""))),
            address=normalize_address(str(row.get("Address", ""))),
            city=normalize_city(str(row.get("City", ""))),
            state=state,
            rack_id=parse_rack_id(row.get("Rack ID")),
            retail_price=retail_price,
            source_row_number=index,
        )
        stats.us_rows += 1
        key = (
            normalized.opis_id,
            normalized.address.casefold(),
            normalized.city.casefold(),
            normalized.state,
        )
        grouped.setdefault(key, []).append(normalized)

    stations: list[NormalizedStation] = []
    for group in grouped.values():
        if len(group) > 1:
            stats.duplicate_groups += 1
            if len({row.retail_price for row in group}) > 1:
                stats.groups_with_price_variance += 1
        stations.append(aggregate_station_group(group))

    stations.sort(key=lambda station: station.opis_id)
    stats.unique_stations = len(stations)
    return stations, stats


def is_us_state(state_code: str) -> bool:
    return state_code in US_STATE_CODES
