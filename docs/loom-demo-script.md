# Loom demo script (≤ 5 minutes)

Record against a local `runserver` with stations imported and a Geoapify key in
`.env`. Keep Postman and the browser map ready before you hit record.

Suggested demo routes (no special-cased code paths):

- Long: **Dallas, TX → Chicago, IL** (~900+ mi, multiple fuel stops)
- Short / map: **Austin, TX → Dallas, TX** (<500 mi)

---

## 0:00–0:30 — Assignment and architecture

“This is a Django REST take-home that plans a US driving route and picks
cost-effective fuel stops for a truck that gets **10 miles per gallon** and can
go **500 miles** on a tank.

Architecture is simple: the API geocodes and routes through **Geoapify**, then
does station matching and fuel optimization **locally** so we don’t burn
provider calls. The Leaflet page is only a viewer — it calls our API, never
Geoapify.”

## 0:30–2:00 — Postman: long route

1. Open the **Fuel Route API** collection / Local environment.
2. Send **Long route (>500 mi) — Dallas to Chicago**.
3. In the JSON response, point out:
   - `route.distance_miles` — clearly over 500
   - `vehicle.mpg` / `max_range_miles` — 10 and 500
   - `fuel_plan.stops` — multiple stops; first has `is_origin_fueling: true`
   - Scan stop `route_mile` values — legs stay within range
   - `fuel_plan.total_gallons` ≈ distance / 10
   - `fuel_plan.estimated_total_cost_usd` — from imported OPIS prices
   - `meta.external_api_calls` — ≤ 3 on a cold text request
4. Optional: resend the same request and show `external_api_calls: 0` and
   `meta.cache` all true.

## 2:00–2:30 — Leaflet map

1. Open `http://127.0.0.1:8000/map/`.
2. Submit **Austin, TX** → **Dallas, TX** (or Dallas → Chicago if you prefer).
3. Show the polyline, start/finish markers, and fuel-stop markers.
4. Open one stop popup: name, price, city/state, route mile, gallons, cost.
5. One line: “This page only hits `/api/v1/routes/plan/`.”

## 2:30–4:15 — Code tour (keep it moving)

| Area | File | What to say |
|---|---|---|
| API surface | `apps/routing/views.py`, `serializers.py` | “Single POST endpoint; strict location input; OpenAPI examples.” |
| Provider | `apps/routing/providers/geoapify.py` | “Geocode + route; call counter; no key in error bodies.” |
| Cache | `apps/routing/services/location.py` | “LocMem geocode/route cache.” |
| Corridor | `apps/routing/services/candidates.py` | “Local Shapely corridor; one DB load; no Geoapify.” |
| Optimizer | `apps/routing/services/optimizer.py` | “Origin fill isn’t free; DP under 500-mile legs.” |

## 4:15–4:40 — Tests

Run or flash:

```bash
uv run pytest -q
```

“About eighty tests — preprocessing, corridor, optimizer, caching, provider
errors, API schema — all with mocked Geoapify.”

## 4:40–5:00 — Efficiency and assumptions

“Text routes: at most two geocodes and one route. Coordinates: one route.
Stations were geocoded offline. Starting fuel is purchased near the origin, not
assumed free. Coordinate accuracy varies for highway-style addresses — that’s
documented in the README.”

Stop. Don’t open Docker or CI unless you still have spare seconds.

---

## Checklist before recording

- [ ] `uv run python manage.py import_fuel_stations` already done
- [ ] `.env` has a working `GEOAPIFY_API_KEY` (never show the key on camera)
- [ ] Postman collection + environment imported
- [ ] Browser tab on `/map/`
- [ ] IDE tabs pre-opened to the five files above
- [ ] Terminal ready with `uv run pytest -q`
