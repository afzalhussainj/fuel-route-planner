# Data files

## Required for clone-and-run

| Path | Purpose |
|---|---|
| `processed/fuel_stations.csv` | Normalized, deduplicated, geocoded stations used by `import_fuel_stations` |
| `processed/geocode_cache.json` | Offline geocode cache used when re-running `prepare_fuel_data` |
| `us_cities.csv` | Secondary USA city gazetteer for small-town geocode fallbacks |

## Employer-supplied source (not committed)

Place the assessment file at:

```text
data/fuel-prices-for-be-assessment.csv
```

It is gitignored because the OPIS retail-price dump may be proprietary and should
not be published if the repository is public. Reviewers receive it with the
assignment brief.

After placing the file:

```bash
uv run python manage.py prepare_fuel_data --skip-geoapify
uv run python manage.py import_fuel_stations
```

Use Geoapify batch geocoding (without `--skip-geoapify`) only when refreshing
coordinates and you have a free Geoapify key with remaining quota.

## Not required at runtime

| Path | Notes |
|---|---|
| `processed/prepare_stats.json` | Local prepare metrics (gitignored) |
| `station_coordinates.json` | Legacy dump; unused by the API (gitignored) |

See `docs/fuel_preprocessing.md` for duplicate-price and accuracy assumptions.
