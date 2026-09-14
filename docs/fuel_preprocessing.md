# Fuel data preprocessing

## Why preprocessing exists

The assessment CSV is not API-ready:

- it includes Canadian rows for a USA-only product
- the same physical station can appear multiple times with different retail prices
- station addresses are often highway/exit descriptions
- the file has no latitude/longitude

We normalize, filter, deduplicate, and geocode **once offline**, then load a
deterministic processed dataset into the application database.

## Why stations are never geocoded per route request

Runtime route planning must stay fast and minimize provider calls
(ideally 1 routing call when coordinates are supplied, or 2 geocode + 1 route
for text locations). Geocoding ~6.6k stations on every request would be slow,
expensive, and non-deterministic. Station coordinates are therefore resolved
only by `prepare_fuel_data` and reused from processed CSV / DB / geocode cache.

## Duplicate-price assumption

Repeated rows that share the same:

- OPIS Truckstop ID
- normalized address
- city
- state

are treated as one physical station.

Observed data: hundreds of such groups carry **different** retail prices and
**no timestamps**. Because we cannot know which observation is newest, the
pipeline stores the **median** `Retail Price`. Canonical station name is the
longest normalized name in the group (stable tie-break).

## Approximate-coordinate behavior

1. Preferred: Geoapify Batch Geocoding (USA filter, batches of ≤1000), resumable
   via `data/processed/geocode_cache.json`.
2. Fallback: city/state centroids via `geonamescache`, with the bundled
   `data/us_cities.csv` as a secondary local gazetteer for small towns missing
   from GeoNames.

Each station is labeled:

- `exact` — high-confidence Geoapify match
- `approximate` — weaker Geoapify match
- `city` — city/state centroid fallback

City centroids are never labeled `exact`. Later optimization should prefer
`exact` coordinates when ranking candidate stops.

## Commands

```bash
# Validate/normalize/dedupe + offline geocode -> processed CSV
uv run python manage.py prepare_fuel_data
# City-fallback only (no Geoapify calls)
uv run python manage.py prepare_fuel_data --skip-geoapify
# Resume / force
uv run python manage.py prepare_fuel_data --force

# Load processed CSV into SQLite (idempotent upserts)
uv run python manage.py import_fuel_stations
```
