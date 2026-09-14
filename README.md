# Fuel Route API

Take-home backend that plans a USA driving route, selects nearby fuel stops from
a preprocessed truck-stop price dataset, and returns a cost-optimized refueling
plan for a fixed vehicle model (10 MPG, 500-mile range).

## Architecture

```text
Client (Postman / Leaflet map)
        │
        ▼
POST /api/v1/routes/plan/   (Django + DRF)
        │
        ├─ resolve start/finish (Geoapify geocode, cached)
        ├─ fetch driving route  (Geoapify routing, cached)
        ├─ select corridor stations (local Shapely / EPSG:5070)
        └─ optimize fuel stops  (local DP; no provider calls)
```

Leaflet at `/map/` is visualization only. The browser never calls Geoapify;
the API key stays server-side.

## Stack

| Piece | Choice |
|---|---|
| Language | Python 3.13 |
| Framework | Django 6.1.1 + Django REST Framework |
| Docs | drf-spectacular (Swagger UI) |
| HTTP client | httpx |
| Geometry | Shapely + pyproj |
| DB | SQLite |
| Map UI | Leaflet 1.9.4 (CDN) |
| Provider | Geoapify (free tier geocode + route) |
| Packaging | uv, Docker (non-root) |

## Setup

### 1. Dependencies

```bash
cp .env.example .env
# Edit .env: set DJANGO_SECRET_KEY and GEOAPIFY_API_KEY
uv sync --group dev
```

### 2. Free Geoapify API key

1. Create a free account at [https://www.geoapify.com/](https://www.geoapify.com/)
2. Open the dashboard → API keys → create a key
3. Put it in `.env` as `GEOAPIFY_API_KEY=...` (server only; never in frontend JS)

### 3. Fuel data

The employer-supplied source CSV is **not** committed (may be proprietary).
Place it at `data/fuel-prices-for-be-assessment.csv`, then either:

**A. Fast path (recommended for reviewers)** — processed stations are already in
the repo:

```bash
uv run python manage.py migrate
uv run python manage.py import_fuel_stations
```

**B. Rebuild from the supplied CSV**

```bash
uv run python manage.py prepare_fuel_data --skip-geoapify
uv run python manage.py import_fuel_stations
```

Details: `data/README.md`, `docs/fuel_preprocessing.md`.

### 4. Run locally

```bash
uv run python manage.py runserver
```

| URL | Purpose |
|---|---|
| http://127.0.0.1:8000/health/ | Health |
| http://127.0.0.1:8000/api/docs/ | Swagger UI |
| http://127.0.0.1:8000/api/schema/ | OpenAPI schema |
| http://127.0.0.1:8000/api/v1/routes/plan/ | Plan endpoint |
| http://127.0.0.1:8000/map/ | Leaflet demo |

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `DJANGO_SECRET_KEY` | Django secret | insecure dev default (must set when `DEBUG=false`) |
| `DJANGO_DEBUG` | Debug mode | `true` |
| `DJANGO_ALLOWED_HOSTS` | Host allowlist | `localhost,127.0.0.1` |
| `GEOAPIFY_API_KEY` | Server-side Geoapify key | empty |
| `GEOAPIFY_BASE_URL` | Provider base URL | `https://api.geoapify.com` |
| `HTTP_*_TIMEOUT_SECONDS` / `HTTP_MAX_RETRIES` | Upstream HTTP policy | 5 connect / 30 read / 2 retries |
| `GEOCODE_CACHE_TTL_SECONDS` | Geocode cache TTL | `86400` |
| `ROUTE_CACHE_TTL_SECONDS` | Route cache TTL | `3600` |
| `VEHICLE_MPG` | MPG | `10` |
| `VEHICLE_MAX_RANGE_MILES` | Max range | `500` |
| `ROUTE_CORRIDOR_MILES` | Base corridor width | `5` |
| `ROUTE_CORRIDOR_MAX_MILES` | Max corridor expansion | `12` |

## Docker

```bash
docker build -t fuel-route-api .
docker run --rm -p 8000:8000 \
  -e DJANGO_SECRET_KEY=change-me-to-a-long-random-string \
  -e DJANGO_DEBUG=false \
  -e DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1 \
  -e SESSION_COOKIE_SECURE=false \
  -e CSRF_COOKIE_SECURE=false \
  -e GEOAPIFY_API_KEY=your-key-here \
  fuel-route-api
```

Entrypoint migrates and imports `data/processed/fuel_stations.csv` when the DB
is empty. Runs as non-root user `app`.

## Endpoint

### `POST /api/v1/routes/plan/`

Accepts text places or coordinates (USA only).

**Example request (text)**

```json
{
  "start": "Dallas, TX",
  "finish": "Chicago, IL"
}
```

**Example request (coordinates)**

```json
{
  "start": {"latitude": 30.2672, "longitude": -97.7431},
  "finish": {"latitude": 32.7767, "longitude": -96.7970}
}
```

**Example response (shape)**

```json
{
  "start": {
    "input": "Dallas, TX",
    "latitude": 32.7767,
    "longitude": -96.797,
    "label": "Dallas, TX, United States of America"
  },
  "finish": {
    "input": "Chicago, IL",
    "latitude": 41.8781,
    "longitude": -87.6298,
    "label": "Chicago, IL, United States of America"
  },
  "route": {
    "distance_miles": 925.4,
    "duration_minutes": 820.5,
    "geometry": {
      "type": "Feature",
      "geometry": {"type": "LineString", "coordinates": []},
      "properties": {}
    }
  },
  "vehicle": {
    "mpg": 10,
    "max_range_miles": 500,
    "tank_capacity_gallons": 50
  },
  "fuel_plan": {
    "total_gallons": 92.54,
    "total_gallons_purchased": 92.54,
    "estimated_total_cost_usd": 287.15,
    "stop_count": 3,
    "stops": []
  },
  "assumptions": [],
  "meta": {
    "external_api_calls": 3,
    "processing_ms": 180,
    "timings_ms": {
      "geocode": 90,
      "route": 70,
      "candidates": 15,
      "optimizer": 2,
      "total": 180
    },
    "fuel_planning": "cost_optimized",
    "cache": {
      "geocode_start": false,
      "geocode_finish": false,
      "route": false
    }
  }
}
```

Swagger: http://127.0.0.1:8000/api/docs/

Postman: `postman/Fuel_Route_API.postman_collection.json` +
`postman/Fuel_Route_API.postman_environment.json`

## Testing

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python manage.py check
```

CI runs the same checks without a live Geoapify key (provider is mocked).

## Design decisions

### External API call strategy

| Input | Uncached budget | Cached repeat |
|---|---|---|
| Text start + finish | ≤ 2 geocode + 1 route | 0 |
| Coordinates | 1 route | 0 |

Station geocoding is offline (`prepare_fuel_data`). Corridor selection and the
fuel optimizer are purely local — they never call the routing API.

### Caching

Django LocMem cache (process-local):

- geocode keys: normalized US place text
- route keys: rounded start/finish coordinates + mode

### Fuel optimization

1. Project the route polyline (EPSG:5070).
2. Keep stations within a corridor (default 5 miles for exact coords; modest
   multipliers for approximate/city accuracy; expand once up to 12 miles if
   coverage gaps exceed 500 miles).
3. Choose an origin fueling station (cheapest in the first 30 route miles,
   expanding to 50 if needed). Starting fuel is **not** free.
4. Run a minimum-cost path over origin → candidates → destination with every
   effective leg ≤ 500 miles (route progress + documented detour allowance).

This is cost-optimal for the fixed route and candidate set, not a claim of
global optimality over all possible paths or detours.

### 500-mile / 10-MPG calculation

- Tank capacity = 500 / 10 = **50 gallons**
- Gallons for a leg = miles / 10
- Cost at a stop = gallons × station `retail_price` from the imported dataset

### Station-coordinate caveats

Many source addresses are highway/exit descriptions. Coordinates may be
`exact`, `approximate`, or `city` centroid. Approximate/city stations use a
wider corridor. See `docs/fuel_preprocessing.md`.

### Performance

On a typical Dallas→Chicago style route with ~6.6k local stations (mocked
provider): candidate selection ~45–50 ms, optimizer ~35–40 ms, total local work
under ~100 ms after routing. Network geocode/route dominate live latency.

## Known limitations

- LocMem cache is per-process (not shared across multiple workers/containers).
- SQLite is fine for the assessment; not a multi-writer production database.
- `runserver` in Docker is for demo only, not production hosting.
- Fuel plan uses corridor candidates on the provider’s fixed route; it does not
  re-route through stations.
- Duplicate source prices without timestamps use a median (documented).
- USA continental bounding box is used for coordinate inputs; Alaska/Hawaii are
  out of scope for this take-home.

## Loom walkthrough

See `docs/loom-demo-script.md` (target ≤ 5 minutes).
