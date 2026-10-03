# Fuel Route API

A Django REST API that plans a road trip inside the USA and the **cheapest places to refuel** along it.
Give it a start and a finish. It returns the route line, the map markers and every cost figure,
ready for a web page to plot with JavaScript (a Leaflet demo page is included at `/`).

It runs in Django with PostgreSQL for the data and Redis for caching and rate limiting. Planning is kept
in plain service modules, so the algorithm can be read and tested without touching HTTP.

## How it works

```
"New York, NY" ──geocode──▶ lat/lon ──OSRM──▶ road route ──grid index──▶ stations within 20 mi ──greedy plan──▶ JSON
               (City table / Nominatim)   (1 call, cached)               of the route (in memory)
```

| Step | Code | Notes |
|---|---|---|
| Prices | [planner/data_loader.py](planner/data_loader.py) | Reads the supplied [fuel-prices-for-be-assessment.csv](fuel-prices-for-be-assessment.csv) directly, one station per OPIS Truckstop ID. |
| Geocode | [planner/services/geocoding.py](planner/services/geocoding.py) | `"City, ST"` / `"City, State"` from the City table (no network); `"lat,lon"` as-is; anything else goes to Nominatim. Cached in Redis. |
| Route | [planner/services/routing.py](planner/services/routing.py) | One OSRM call, asking for `annotations=distance` so every vertex carries its true road distance. The cache entry holds the polyline *and* the thinned route, so a warm request decodes nothing. |
| Stations | [planner/services/stations.py](planner/services/stations.py) | In-memory 0.25° grid of all stations. Stations are walked against the route points near them, not the reverse: most cells along a route are empty. |
| Plan | [planner/services/fuel_plan.py](planner/services/fuel_plan.py) | Greedy rule: "buy just enough to reach a cheaper station, otherwise fill up." An exact dynamic program over the same stations confirms this reaches the true optimum once `MIN_SAVING_PER_GAL` and `MIN_HOP_MILES` are zero; those two knobs trade under 1% of cost for a shorter stop list. |
| Response | [planner/services/trip.py](planner/services/trip.py) | Builds the JSON below. |

Assumptions: a 500-mile range, 10 MPG, a full tank at departure, and stations up to 20 miles off the
route (configured in `FUELROUTE` in [config/settings.py](config/settings.py)).

The planner will use the whole 500 miles, as the brief specifies. `RESERVE_MILES` can hold some back
so every leg finishes with fuel to spare, which is closer to how a driver behaves given that station
positions here are only city-accurate, but it is 0 by default: a reserve would refuse a 490-mile gap
the vehicle can in fact clear.

**Stations are treated as lying on the route.** 97% of the rows in the price file give a highway
address and 56% name an interstate exit, so these are highway truck stops that really are along the
way. The file has no pump coordinates, only a city, so a station's distance from the route line is the
distance to its city centre, not to the pump. Billing that as a detour would charge fuel for geocoding
error rather than for driving. The response still reports it, as `distance_from_route_miles`.

That is also why the corridor is 20 miles. It is not a detour budget; it is how far a city centre can
sit from the route before its truck stop stops being plausibly on that road. A tighter corridor would
claim precision the data does not have, and it costs coverage where stations are sparse: on Salt Lake
City to Reno the longest gap between usable stations falls from 470 miles at a 10-mile corridor to 321
at 20, useful headroom against a 500-mile tank.

## Data files

| File | What it is |
|---|---|
| [fuel-prices-for-be-assessment.csv](fuel-prices-for-be-assessment.csv) | The supplied price list. The authority on which truck stops exist and what they charge. |
| [us-cities.csv](us-cities.csv) | 29,738 US cities with coordinates. Geocodes `"City, ST"` input offline, and places each truck stop, since the price file has no coordinates. |
| [city-supplement.json](city-supplement.json) | Coordinates and spelling aliases for the cities the price file names that `us-cities.csv` does not cover. |

All three are read at load time only. Nothing here is served to a browser.

## Setup

Requirements: Python 3.12+, PostgreSQL, Redis (Docker is fine).

```powershell
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt

copy .env.example .env          # then edit: secret key, DB password, etc.
docker compose up -d redis      # Redis on 127.0.0.1:${REDIS_PORT}

# create the database once (psql or pgAdmin):  CREATE DATABASE fuelroute;
python manage.py migrate        # creates tables AND seeds cities + fuel stations
python manage.py create_api_key "my frontend"   # prints the key once (or set DEMO_API_KEY in .env)
python manage.py createsuperuser                # optional, for /admin/
python manage.py runserver
```

Open http://127.0.0.1:8000/ and plan a route (the key field is pre-filled if `DEMO_API_KEY` is set).

**Tests:** `python manage.py test` (64 tests). They need no network and no Redis: OSRM is mocked, the cache is
in memory, and a broken-cache double checks that an outage degrades rather than fails.

## Where the fuel prices come from

[fuel-prices-for-be-assessment.csv](fuel-prices-for-be-assessment.csv) is the authority on which truck
stops exist and what they charge. `manage.py migrate` loads it; `manage.py load_data` reloads it after
the file changes, and running servers pick the new data up within a few seconds.

**Every truck stop in the file reaches the planner.** Nothing is filtered out. The accounting:

| | Count |
|---|---|
| Rows in the CSV | 8151 |
| Distinct OPIS Truckstop IDs | 6738 |
| ... in US states | 6626 |
| ... in Canadian provinces | 112 |
| **Loaded into the planner** | **6738** |

Verify it at any time with `python manage.py check_prices`, which reports how many truck stops could
not be placed on a map and names the cities behind them. That report is currently empty, and a test
asserts it stays that way.

Three decisions worth knowing, each visible in [planner/data_loader.py](planner/data_loader.py):

- **A truck stop is one OPIS Truckstop ID.** 678 ids appear on more than one row, almost always a long
  and a short form of the same brand name at the same address. Keying on the name instead would turn
  one site into two.
- **Repeat listings keep the lowest price and the fullest name.** 597 of those pairs disagree on price,
  by $0.09 at the median, and the file carries no date to tell them apart. The lower figure is a real
  quoted price for that site and this is a planner for cheap fuel. `DUPLICATE_PRICE` in the loader
  switches it to `max` or an average.
- **Stations sit at the centre of the city they name.** The price file has a city and state but no
  coordinates, which is why the planner looks for stations within a corridor of the route rather than
  at an exact point, and why that corridor is 20 miles rather than something tighter.

Only two kinds of row are skipped, and both are counted in the log: a row with no state at all, and a
row whose price will not parse. The supplied file has neither.

The price file names 91 cities that [us-cities.csv](us-cities.csv) does not cover, 85 of them
Canadian, which would have left those truck stops unplaceable.
[city-supplement.json](city-supplement.json) closes the gap. Two are spelling variants of a city
already in the table; the other 89 carry coordinates from Nominatim, each accepted only when the
ISO 3166-2 subdivision it returned matched the state or province the price file asked for, so a
same-named place elsewhere is never used. Regenerate it with
`python manage.py check_prices --geocode-missing`.

Trip endpoints are still US-only, as the brief requires. Including the Canadian stations only means a
route running close to the border can consider one, the same way it would any other station within the
corridor.

Reloading matches rows on the OPIS id, so prices update in place and no station changes its
`station_id`. That is what makes the id in a response worth storing.

## API reference

All endpoints live under `/api/v1/`. Responses are JSON.

### Authentication

Send your key in a header:

```
X-API-Key: fr_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Create keys with `manage.py create_api_key` or in `/admin/` (the key is shown once). Deactivate or expire them in the admin.

**Demo key:** set `DEMO_API_KEY` in `.env` (at least 32 characters) and the API accepts it with no setup step.
The demo page at `/` also pre-fills it. The first time it is used, it is saved as a normal key (hash only),
so rate limits apply and you can deactivate it in the admin. Anyone who opens `/` can read it,
so leave it empty in real deployments.

### `GET /api/v1/route/` (or `POST` with a JSON body)

| Parameter | Required | Description |
|---|---|---|
| `from` | yes | `"City, ST"`, `"City, State"`, `"lat,lon"` or a street address (2–200 chars) |
| `to` | yes | same as `from` |
| `geometry` | no | `polyline` (default): an encoded string, ~6× smaller; `points`: `[[lat, lon], …]`, ready to plot |

```bash
curl -H "X-API-Key: $KEY" -G "http://127.0.0.1:8000/api/v1/route/" \
     --data-urlencode "from=New York, NY" --data-urlencode "to=Los Angeles, CA"
```

Response (shortened):

```jsonc
{
  "from": {"query": "New York, NY", "name": "New York, NY", "lat": 40.75, "lon": -74.0, "source": "city_table"},
  "to":   {"query": "Los Angeles, CA", "name": "Los Angeles, CA", "lat": 33.97, "lon": -118.25, "source": "city_table"},
  "summary": {
    "distance_miles": 2801.2,
    "duration_seconds": 179687, "fuel_used_gallons": 280.12,
    "fuel_stops": 10, "total_cost_usd": 848.87,
    "starting_tank": {"gallons": 50.0, "price_per_gallon": 3.059, "price_source": "first_stop", "cost_usd": 152.95},
    "fuel_bought_on_route_usd": 695.92,
    // Fuel still in the tank on arrival, refunded at what was actually paid for it.
    "leftover_fuel_gallons": 0.0, "leftover_fuel_credit_usd": 0.00
  },
  "stops": [
    {"sequence": 1, "station_id": 25965, "name": "SHEETZ #639", "city": "Youngstown", "state": "OH",
     "lat": 41.1, "lon": -80.65, "mile": 392.6,
     "distance_from_route_miles": 3.7,   // to the city centre; reported, not charged
     "price_per_gallon": 3.059, "fuel_on_arrival_gallons": 10.74, "gallons": 5.61, "cost_usd": 17.16}
  ],
  "markers": [                       // everything to pin on the map, in order
    {"type": "start", "lat": 40.75, "lon": -74.0, "label": "New York, NY"},
    {"type": "fuel_stop", "lat": 41.1, "lon": -80.65, "label": "1. SHEETZ #639", "sequence": 1},
    {"type": "finish", "lat": 33.97, "lon": -118.25, "label": "Los Angeles, CA"}
  ],
  "route": {"format": "polyline", "point_count": 34788, "precision": 5,
            "bounds": [[33.97, -118.25], [41.4, -74.0]],   // pass to map.fitBounds()
            "polyline": "mnwwFjtzbMvBlk@..."},             // geometry=points gives "points" instead
  "assumptions": {"vehicle_range_miles": 500, "miles_per_gallon": 10, "station_corridor_miles": 20,
                  "reserve_miles": 0, "stations_are_on_route": true, ...},
  "meta": {"route_cached": false, "stations_considered": 616,
           "distance_source": "osrm",      // "estimated" if OSRM sent no distance annotations
           "cache_degraded": false,        // true while the cache is unreachable
           "timings_ms": {"geocode": 39.5, "route": 1754.0, "plan": 27.1}},
  "request_id": "0d96f7edf4d043f2b5d7dcc1924d7834"
}
```

`total_cost_usd` is exactly `starting_tank.cost_usd + fuel_bought_on_route_usd - leftover_fuel_credit_usd`.
Prices and money are computed as decimals, so the parts always add up to the total to the cent.

Plotting it with Leaflet:

```js
// The default geometry is the encoded polyline; ask for geometry=points to skip decoding.
const line = data.route.points ?? L.PolylineUtil.decode(data.route.polyline);  // or any decoder
L.polyline(line).addTo(map);
data.markers.forEach(m => L.marker([m.lat, m.lon]).bindPopup(m.label).addTo(map));
map.fitBounds(data.route.bounds);
```

[planner/static/planner/demo.js](planner/static/planner/demo.js) has a 15-line decoder if you would
rather not add a dependency.

### `GET /api/v1/health/`

Public (no key), on its own generous rate limit so a fleet of probes sharing one source address cannot
throttle itself into looking unhealthy. Returns `200 {"status": "ok", "checks": {"database": "ok", "cache": "ok"}}`.

Only the database is required. Without it nothing can be planned, so that is a `503`. The cache is not:
the API runs without it, just slower, so a cache failure answers `200` with `"status": "degraded"` and
`"checks": {"cache": "error"}`.

### Errors

Every error has the same shape:

```json
{"error": {"code": "validation_error", "message": "Invalid input.",
           "details": {"from": ["This field is required."]}, "request_id": "c400…"}}
```

| HTTP | `code` | When |
|---|---|---|
| 400 | `validation_error`, `parse_error` | Missing or invalid fields; malformed JSON |
| 401 | `not_authenticated`, `authentication_failed` | Missing, unknown, inactive or expired key |
| 404 | `not_found` | Unknown URL |
| 422 | `location_not_found` | Place not found, or outside the USA |
| 422 | `route_not_found` | No road connects the two points |
| 422 | `no_station_in_range` | A gap longer than the vehicle range has no station |
| 429 | `rate_limited` | Too many requests (see the `Retry-After` header) |
| 429 | `too_many_auth_failures` | Too many bad keys from this IP (10-minute block) |
| 503 | `upstream_unavailable` | OSRM or Nominatim failed or timed out |
| 500 | `internal_error` | Bug (details only in the server log; quote `request_id`) |

## Security

- **API keys**: 256-bit random; only the SHA-256 hash is stored; keys support expiry and deactivation.
- **Brute-force protection**: after 20 bad keys from one IP within 10 minutes, further *invalid* keys from
  it are refused. A valid key still works: clients behind one NAT address or proxy share an IP, so
  blocking the address would let any attacker lock out everyone else.
- **Rate limits** per key (default `5/second` burst, `30/minute` sustained), stored in Redis so they are shared across processes.
- **Input validation**: length limits, a character allow-list, and coordinates restricted to the USA. Request bodies are capped at 10 KB, and only JSON is accepted.
- **No SSRF**: upstream URLs come from settings only. User text is sent only as encoded query parameters. Upstream calls use timeouts, no redirects, and a 10 MB response cap.
- **Headers**: `Content-Security-Policy`, `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, and `Cache-Control: no-store` on API responses. HSTS, SSL redirect and secure cookies are on when `DJANGO_DEBUG=False`.
  `Referrer-Policy` is `strict-origin-when-cross-origin`, which sends only the scheme and host to a
  third party and never the path or query, so typed locations do not leak. It is deliberately not
  `same-origin`: that sends no Referer at all, and OpenStreetMap's tile servers answer 403 to map
  requests that carry nothing identifying the app.
- **CORS**: an explicit origin allow-list, applied to `/api/` only, with no credentials.
- **Errors** never expose stack traces. Every response carries an `X-Request-ID` that matches the server logs, and logs omit query strings (user locations).
- **Secrets** live in `.env` (git-ignored). The app refuses to start without `DJANGO_SECRET_KEY`.

### When Redis is unavailable

The cache is an accelerator, not a source of truth: the station index is in process memory, cities are in
PostgreSQL, and OSRM and Nominatim can always be asked again. So an outage costs speed, not availability.
Every cache read and write degrades to a miss, `meta.cache_degraded` turns true in each response, and the
health check reports `"degraded"` while still answering 200.

Two things to know. Rate limiting and the brute-force counter stop being enforced, because both count in
the cache and have nowhere to write. The throttles fail open rather than raising, which is deliberate:
refusing all traffic because the rate limiter is down is worse than not counting for a while. API keys
are still verified against PostgreSQL, so an outage does not let anyone in. And Nominatim's one-request-per-second
limit falls back to being spaced out per process instead of across the fleet, so run fewer processes or
self-host Nominatim if an outage is likely to last.

### Production checklist

- `DEMO_API_KEY` empty (otherwise the key is published on the demo page).
- `DJANGO_DEBUG=False`, a long random `DJANGO_SECRET_KEY`, real `DJANGO_ALLOWED_HOSTS` and `CORS_ALLOWED_ORIGINS`.
- Run with a WSGI server (e.g. gunicorn/waitress) behind a TLS-terminating proxy that sets `X-Forwarded-Proto`, serves `/static/` (after `collectstatic`), and sets `REST_FRAMEWORK["NUM_PROXIES"]` so client IPs are correct.
- Give Redis a password, or keep it on a private network.
- Set `NOMINATIM_USER_AGENT` with a real contact (placeholders get HTTP 403). For heavy use, self-host OSRM/Nominatim (`OSRM_URL`, `NOMINATIM_URL`).
