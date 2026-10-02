"""
Tests for the fuel route API. Run with:  python manage.py test

No network is used: OSRM is replaced by a fake that returns a straight east-west route,
and the cache is an in-memory one (see TESTING in settings.py).
"""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .exceptions import NoStationInRange, UpstreamUnavailable
from .models import APIKey, City, FuelStation
from .services import safe_cache
from .services.fuel_plan import plan_fuel_stops
from .services.routing import decode_polyline, thin_route
from .services.stations import Candidate, bump_data_version
from .throttling import HealthThrottle

ROUTE_URL = "/api/v1/route/"
METERS_PER_MILE = 1609.344


def encode_polyline(points, precision=5):
    """Inverse of decode_polyline, used to build fake OSRM responses."""
    factor = 10 ** precision
    out, prev_lat, prev_lon = [], 0, 0
    for lat, lon in points:
        ilat, ilon = round(lat * factor), round(lon * factor)
        for delta in (ilat - prev_lat, ilon - prev_lon):
            v = ~(delta << 1) if delta < 0 else delta << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


def fake_osrm(lat, lon_start, lon_end, miles, annotate=True):
    """
    A mock for routing.get_json returning a straight route along one latitude.

    With annotate=True it also sends per-segment road distances, the way a real server does
    for `annotations=distance`, so the planner's exact-distance path is what gets exercised.
    """
    steps = 2000
    points = [(lat, lon_start + (lon_end - lon_start) * i / steps) for i in range(steps + 1)]
    leg = {}
    if annotate:
        per_segment = miles * METERS_PER_MILE / steps
        leg["annotation"] = {"distance": [per_segment] * steps}
    body = {
        "code": "Ok",
        "routes": [{
            "distance": miles * METERS_PER_MILE,
            "duration": miles * 60,
            "geometry": encode_polyline(points),
            "legs": [leg],
        }],
    }
    return mock.patch("planner.services.routing.get_json", return_value=(200, body))


def broken_cache():
    """Patch the cache backend so every operation raises, standing in for a Redis outage."""
    backend = mock.MagicMock()
    for operation in ("get", "set", "add", "incr", "delete"):
        getattr(backend, operation).side_effect = ConnectionError("cache is down")
    return mock.patch("planner.services.safe_cache.cache", backend)


def candidate(mile, price, offset=0.0):
    """A minimal station for fuel-plan unit tests."""
    return Candidate(id=int(mile), name=f"S{mile}", city="X", state="TX", lat=0, lon=0,
                     price=price, mile=mile, offset=offset)


# reserve_miles defaults to 0 here so the arithmetic in each test stays easy to follow;
# ReserveTests covers the configured reserve explicitly.
PLAN_ARGS = dict(range_miles=500, mpg=10, min_saving=0.03, min_hop=100, reserve_miles=0.0)


class PolylineTests(TestCase):
    """The decoder must match Google's reference example and refuse broken input."""

    def test_decode_reference_example(self):
        """Google's documented sample decodes to its three documented points."""
        lats, lons = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
        self.assertEqual(list(zip(lats, lons)), [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)])

    def test_round_trip(self):
        """encode -> decode returns the original points."""
        pts = [(35.0, -100.0), (35.12345, -99.54321), (34.9, -98.0)]
        lats, lons = decode_polyline(encode_polyline(pts))
        self.assertEqual(list(zip(lats, lons)), pts)

    def test_truncated_polyline_is_an_upstream_error(self):
        """A cut-off string is a bad upstream answer, not an IndexError and a 500."""
        full = encode_polyline([(35.0, -100.0), (35.5, -99.0), (36.0, -98.0)])
        with self.assertRaises(UpstreamUnavailable):
            decode_polyline(full[:-1])


class ThinRouteTests(TestCase):
    """Distance along the route, with and without OSRM's own per-segment distances."""

    def test_uses_exact_segment_distances(self):
        """Given real road distances, cumulative miles are those distances, not chords."""
        # A zig-zag whose straight-line length is far short of its stated road length.
        lats = [35.0, 35.2, 35.0, 35.2, 35.0]
        lons = [-100.0, -99.5, -99.0, -98.5, -98.0]
        segments = [25.0, 25.0, 25.0, 25.0]   # 100 road miles in total
        _, _, cum = thin_route(lats, lons, 100.0, segments)
        self.assertAlmostEqual(cum[-1], 100.0, places=6)
        # Monotonic, and every kept point carries the full road distance to reach it.
        self.assertEqual(cum, sorted(cum))

    def test_falls_back_to_rescaled_chords(self):
        """Without annotations the chord sum is rescaled to the trip total."""
        lats = [35.0, 35.2, 35.0, 35.2, 35.0]
        lons = [-100.0, -99.5, -99.0, -98.5, -98.0]
        _, _, cum = thin_route(lats, lons, 100.0, None)
        self.assertAlmostEqual(cum[-1], 100.0, places=6)

    def test_empty_route(self):
        """No vertices in, nothing out, no exception."""
        self.assertEqual(thin_route([], [], 0.0), ([], [], []))


class FuelPlanTests(TestCase):
    """The greedy refuelling algorithm, without any HTTP or database."""

    def test_short_trip_needs_no_stop(self):
        """Under 500 miles: no stops, starting tank billed at the cheapest price on the route."""
        plan = plan_fuel_stops([candidate(100, 3.0), candidate(200, 2.5)], 300, **PLAN_ARGS)
        self.assertEqual(plan.stops, [])
        self.assertEqual(plan.start_price, Decimal("2.5"))
        self.assertEqual(plan.total_cost, Decimal("75.00"))

    def test_short_trip_without_stations_uses_fallback_price(self):
        """No stations at all: the national average is used and reported as such."""
        plan = plan_fuel_stops([], 300, fallback_price=Decimal("3.0"), **PLAN_ARGS)
        self.assertEqual(plan.start_price_source, "national_average")
        self.assertEqual(plan.total_cost, Decimal("90.00"))

    def test_long_trip_buys_fuel(self):
        """800 miles with stations every 100 miles: at least one purchase, and costs add up."""
        stations = [candidate(m, 3.0 + (m % 300) / 1000) for m in range(100, 800, 100)]
        plan = plan_fuel_stops(stations, 800, **PLAN_ARGS)
        self.assertGreaterEqual(len(plan.stops), 1)
        bought = sum((s.cost for s in plan.stops), Decimal("0.00"))
        self.assertEqual(plan.total_cost, plan.start_cost - plan.leftover_credit + bought)

    def test_gap_too_large_raises(self):
        """No station in the first 500 miles of a 900-mile trip: impossible."""
        with self.assertRaises(NoStationInRange):
            plan_fuel_stops([candidate(600, 3.0)], 900, **PLAN_ARGS)

    def test_every_reported_stop_buys_fuel(self):
        """A station we merely drive past is not reported as a fuel stop."""
        stations = [candidate(m, 3.0) for m in range(100, 1200, 100)]
        plan = plan_fuel_stops(stations, 1200, **PLAN_ARGS)
        self.assertTrue(all(s.gallons > 0 for s in plan.stops))

    def test_money_is_exact_to_the_cent(self):
        """Costs are Decimal, so the reported parts sum to the reported total exactly."""
        stations = [candidate(m, Decimal("3.117") + Decimal(m % 4) / 100)
                    for m in range(100, 1500, 100)]
        plan = plan_fuel_stops(stations, 1500, **PLAN_ARGS)
        bought = sum((s.cost for s in plan.stops), Decimal("0.00"))
        self.assertEqual(plan.total_cost, plan.start_cost - plan.leftover_credit + bought)
        for stop in plan.stops:
            self.assertEqual(stop.cost.as_tuple().exponent, -2)


class DetourTests(TestCase):
    """Stations sit off the route, and leaving the route to reach one costs fuel."""

    def test_detour_miles_are_charged(self):
        """A stop 8 miles off the route adds 16 miles of driving, out and back."""
        stations = [candidate(m, 3.0, offset=8.0) for m in range(100, 900, 100)]
        plan = plan_fuel_stops(stations, 900, **PLAN_ARGS)
        self.assertGreater(len(plan.stops), 0)
        self.assertAlmostEqual(plan.detour_miles, 16.0 * len(plan.stops))
        self.assertAlmostEqual(plan.driven_miles, 900 + plan.detour_miles)

    def test_stations_on_the_route_add_no_detour(self):
        """With offset 0 the driven distance is just the route."""
        stations = [candidate(m, 3.0) for m in range(100, 900, 100)]
        plan = plan_fuel_stops(stations, 900, **PLAN_ARGS)
        self.assertEqual(plan.detour_miles, 0.0)
        self.assertEqual(plan.driven_miles, 900)

    def test_detour_eats_into_range(self):
        """A station reachable on the route alone can be out of reach once its detour counts."""
        on_route = plan_fuel_stops([candidate(495, 3.0)], 900, **PLAN_ARGS)
        self.assertEqual(len(on_route.stops), 1)
        # The same station 10 miles off the route needs 505 miles of range to get to.
        with self.assertRaises(NoStationInRange):
            plan_fuel_stops([candidate(495, 3.0, offset=10.0)], 900, **PLAN_ARGS)

    def test_detour_raises_the_bill(self):
        """Charging the detour costs more than ignoring it did."""
        near = plan_fuel_stops([candidate(m, 3.0) for m in range(100, 900, 100)], 900, **PLAN_ARGS)
        far = plan_fuel_stops([candidate(m, 3.0, offset=9.0) for m in range(100, 900, 100)],
                              900, **PLAN_ARGS)
        self.assertGreater(far.total_cost, near.total_cost)


class ReserveTests(TestCase):
    """No leg is planned to end with a dry tank."""

    def test_arrivals_keep_the_reserve(self):
        """Every stop is reached with at least reserve_miles of range left."""
        args = {**PLAN_ARGS, "reserve_miles": 25.0}
        stations = [candidate(m, 3.0 + (m % 500) / 1000) for m in range(100, 2000, 100)]
        plan = plan_fuel_stops(stations, 2000, **args)
        self.assertGreater(len(plan.stops), 1)
        # The first stop is reached on the starting tank; the rest on fuel we chose to buy.
        for stop in plan.stops[1:]:
            self.assertGreaterEqual(stop.arrive_miles, 25.0 - 1e-6)

    def test_reserve_shortens_the_longest_leg(self):
        """A station exactly one tank away is out of reach once a reserve is required."""
        stations = [candidate(500, 3.0), candidate(900, 3.0)]
        self.assertEqual(len(plan_fuel_stops(stations, 1200, **PLAN_ARGS).stops), 2)
        with self.assertRaises(NoStationInRange):
            plan_fuel_stops(stations, 1200, **{**PLAN_ARGS, "reserve_miles": 25.0})

    def test_reserve_must_be_smaller_than_range(self):
        """A reserve as large as the tank is a configuration error, caught loudly."""
        with self.assertRaises(ValueError):
            plan_fuel_stops([candidate(100, 3.0)], 900,
                            **{**PLAN_ARGS, "reserve_miles": 500.0})


class LeftoverCreditTests(TestCase):
    """
    Fuel left at the destination is credited at what was actually paid for it.

    With no reserve the greedy buys exactly enough to arrive, so nothing is left over and the
    credit never comes up. A reserve is what puts fuel in the tank at the finish.
    """

    RESERVE_ARGS = {**PLAN_ARGS, "reserve_miles": 25.0}

    def test_credit_uses_the_blended_tank_price(self):
        """
        The tank holds cheap fuel bought earlier and expensive fuel bought last.

        Crediting the leftover at the last station's price, which is what the code used to do,
        would refund more than was ever spent on that fuel. The credit must land between the
        two prices.
        """
        stations = [candidate(100, Decimal("2.000")), candidate(550, Decimal("9.000"))]
        plan = plan_fuel_stops(stations, 620, **self.RESERVE_ARGS)
        self.assertGreater(plan.leftover_gallons, 0)
        implied = plan.leftover_credit / Decimal(repr(plan.leftover_gallons))
        self.assertLess(implied, Decimal("9.000"))
        self.assertGreater(implied, Decimal("2.000"))

    def test_credit_never_exceeds_what_was_spent(self):
        """The refund cannot be larger than the fuel bill it is deducted from."""
        stations = [candidate(m, Decimal("3.000")) for m in range(100, 1400, 100)]
        plan = plan_fuel_stops(stations, 1400, **self.RESERVE_ARGS)
        spent = plan.start_cost + sum((s.cost for s in plan.stops), Decimal("0.00"))
        self.assertLessEqual(plan.leftover_credit, spent)
        self.assertGreaterEqual(plan.total_cost, Decimal("0.00"))

    def test_leftover_matches_the_reserve(self):
        """Arriving with the reserve intact means exactly that much fuel is credited back."""
        stations = [candidate(m, Decimal("3.000")) for m in range(100, 1400, 100)]
        plan = plan_fuel_stops(stations, 1400, **self.RESERVE_ARGS)
        self.assertAlmostEqual(plan.leftover_gallons, 2.5, places=6)


class APITestBase(TestCase):
    """Creates a key, a few cities and a line of stations along latitude 35°N."""

    def setUp(self):
        """Fresh cache, data and client for every test."""
        cache.clear()
        safe_cache.reset_for_tests()
        api_key = APIKey(name="test")
        self.raw_key = api_key.set_new_key()
        api_key.save()
        self.api_key = api_key

        # The test DB is seeded by migration 0002; replace that with small, predictable data.
        City.objects.all().delete()
        FuelStation.objects.all().delete()
        City.objects.create(name="west town", state="ok", lat=35.0, lon=-100.0)
        City.objects.create(name="east town", state="tn", lat=35.0, lon=-85.0)
        City.objects.create(name="st louis", state="mo", lat=38.63, lon=-90.2)
        # A station roughly every 0.5° of longitude (~28 mi), prices varying.
        for i in range(31):
            lat, lon = 35.01, -100 + i * 0.5
            FuelStation.objects.create(
                name=f"Station {i}", city=f"Town {i}", state="TX", lat=lat, lon=lon,
                price=Decimal("3.000") + Decimal(i % 7) / 10,
                external_key=f"opis:test-{i}")
        bump_data_version()  # make the in-memory station index reload

        self.client = APIClient()
        self.client.credentials(HTTP_X_API_KEY=self.raw_key)


class AuthTests(APITestBase):
    """API-key authentication and brute-force protection."""

    def test_missing_key_is_401(self):
        """No X-API-Key header -> 401 with the standard error body."""
        res = APIClient().get(ROUTE_URL, {"from": "West Town, OK", "to": "East Town, TN"})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "not_authenticated")
        self.assertIn("request_id", res.json()["error"])

    def test_wrong_key_is_401(self):
        """An unknown key is rejected."""
        client = APIClient()
        client.credentials(HTTP_X_API_KEY="fr_wrong")
        res = client.get(ROUTE_URL, {"from": "West Town, OK", "to": "East Town, TN"})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "authentication_failed")

    def test_inactive_and_expired_keys_are_rejected(self):
        """Deactivated or expired keys behave exactly like unknown keys."""
        for change in ({"is_active": False}, {"expires_at": timezone.now() - timedelta(days=1)}):
            APIKey.objects.filter(pk=self.api_key.pk).update(**{"is_active": True, "expires_at": None, **change})
            res = self.client.get("/api/v1/route/", {"from": "a b", "to": "c d"})
            self.assertEqual(res.status_code, 401)

    def test_repeated_bad_keys_are_blocked(self):
        """Past the limit, further invalid keys from that IP get 429 instead of 401."""
        bad = APIClient()
        bad.credentials(HTTP_X_API_KEY="fr_guess")
        with self.settings(FUELROUTE={**self._fuelroute(), "MAX_AUTH_FAILURES": 3}):
            codes = [bad.get(ROUTE_URL).status_code for _ in range(4)]
            blocked = bad.get(ROUTE_URL).json()
        self.assertEqual(codes, [401, 401, 429, 429])
        self.assertEqual(blocked["error"]["code"], "too_many_auth_failures")

    def test_a_valid_key_survives_someone_elses_failures(self):
        """
        The brute-force counter gates guessing, not the legitimate client.

        Clients behind one NAT address or proxy share an IP, so blocking the address would
        let any attacker lock out everyone else.
        """
        bad = APIClient()
        bad.credentials(HTTP_X_API_KEY="fr_guess")
        with self.settings(FUELROUTE={**self._fuelroute(), "MAX_AUTH_FAILURES": 3}):
            for _ in range(5):
                bad.get(ROUTE_URL)
            # Same IP, valid key: reaches validation (400), so it authenticated fine.
            res = self.client.get(ROUTE_URL)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "validation_error")

    def test_demo_key_from_env(self):
        """DEMO_API_KEY works without being created first, is saved hashed, and can be deactivated."""
        demo = "fr_" + "d" * 40
        client = APIClient()
        client.credentials(HTTP_X_API_KEY=demo)
        with self.settings(FUELROUTE={**self._fuelroute(), "DEMO_API_KEY": demo}):
            self.assertEqual(client.get(ROUTE_URL).status_code, 400)  # authenticated -> reaches validation
            row = APIKey.objects.get(hashed_key=APIKey.hash(demo))
            self.assertEqual(row.name, "Demo key (from .env)")

            row.is_active = False
            row.save()
            self.assertEqual(client.get(ROUTE_URL).status_code, 401)

            # The demo page pre-fills it.
            self.assertContains(self.client.get("/"), f'value="{demo}"')

    def test_demo_key_disabled_when_empty(self):
        """With DEMO_API_KEY empty, nothing new is accepted."""
        client = APIClient()
        client.credentials(HTTP_X_API_KEY="")
        with self.settings(FUELROUTE={**self._fuelroute(), "DEMO_API_KEY": ""}):
            self.assertEqual(client.get(ROUTE_URL).status_code, 401)

    def test_only_hash_is_stored(self):
        """The raw key is never saved in the database."""
        self.assertNotIn(self.raw_key, str(APIKey.objects.values_list().get(pk=self.api_key.pk)))

    @staticmethod
    def _fuelroute():
        """Current FUELROUTE settings (to override one value)."""
        from django.conf import settings
        return settings.FUELROUTE


class ValidationTests(APITestBase):
    """Bad input is rejected with 400 and per-field details."""

    def test_missing_fields(self):
        """Both 'from' and 'to' are required."""
        res = self.client.get(ROUTE_URL)
        self.assertEqual(res.status_code, 400)
        body = res.json()["error"]
        self.assertEqual(body["code"], "validation_error")
        self.assertIn("from", body["details"])
        self.assertIn("to", body["details"])

    def test_bad_characters_and_length(self):
        """Script tags and over-long strings never reach the services."""
        for bad in ("<script>alert(1)</script>", "x" * 201):
            res = self.client.get(ROUTE_URL, {"from": bad, "to": "East Town, TN"})
            self.assertEqual(res.status_code, 400, bad)

    def test_same_start_and_finish(self):
        """A trip needs two different places."""
        res = self.client.get(ROUTE_URL, {"from": "West Town, OK", "to": "west town, ok"})
        self.assertEqual(res.status_code, 400)

    def test_bad_geometry_choice(self):
        """geometry must be 'points' or 'polyline'."""
        res = self.client.get(ROUTE_URL, {"from": "West Town, OK", "to": "East Town, TN", "geometry": "svg"})
        self.assertEqual(res.status_code, 400)

    def test_coordinates_outside_usa(self):
        """Coordinates are accepted, but only inside the USA."""
        res = self.client.get(ROUTE_URL, {"from": "48.85,2.35", "to": "East Town, TN"})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error"]["code"], "location_not_found")


class RoutePlanTests(APITestBase):
    """End-to-end: request -> geocode -> (fake) OSRM -> stations -> plan -> JSON."""

    def test_full_response(self):
        """A ~850 mile trip returns stops, markers, a route and consistent totals."""
        with fake_osrm(35.0, -100.0, -85.0, 850) as osrm:
            res = self.client.get(ROUTE_URL, {"from": "West Town, OK", "to": "East Town, TN"})

        self.assertEqual(res.status_code, 200, res.content)
        data = res.json()
        osrm.assert_called_once()

        self.assertEqual(data["from"]["source"], "city_table")
        self.assertEqual(data["from"]["name"], "West Town, OK")
        self.assertEqual(data["summary"]["distance_miles"], 850.0)
        self.assertGreaterEqual(data["summary"]["fuel_stops"], 1)
        self.assertEqual(len(data["stops"]), data["summary"]["fuel_stops"])

        # Markers: start, one per stop, finish.
        types = [m["type"] for m in data["markers"]]
        self.assertEqual(types[0], "start")
        self.assertEqual(types[-1], "finish")
        self.assertEqual(types.count("fuel_stop"), len(data["stops"]))

        # Route as the encoded polyline by default, with bounds for fitBounds().
        self.assertEqual(data["route"]["format"], "polyline")
        self.assertIn("polyline", data["route"])
        self.assertEqual(data["route"]["point_count"], 2001)
        self.assertEqual(data["route"]["bounds"], [[35.0, -100.0], [35.0, -85.0]])

        # Distances come from OSRM's own annotations, not from rescaled straight lines.
        self.assertEqual(data["meta"]["distance_source"], "osrm")
        self.assertFalse(data["meta"]["cache_degraded"])

        # Totals add up (to the cent).
        s = data["summary"]
        expected = s["starting_tank"]["cost_usd"] + s["fuel_bought_on_route_usd"] - s["leftover_fuel_credit_usd"]
        self.assertAlmostEqual(s["total_cost_usd"], expected, delta=0.005)

        # Driving includes the detours to each pump.
        self.assertGreaterEqual(s["driven_miles"], s["distance_miles"])
        self.assertAlmostEqual(s["driven_miles"], s["distance_miles"] + s["detour_miles"], delta=0.05)
        self.assertAlmostEqual(s["fuel_used_gallons"], s["driven_miles"] / 10, delta=0.05)

        # Response headers.
        self.assertEqual(res["Cache-Control"], "no-store")
        self.assertTrue(res["X-Request-ID"])
        self.assertEqual(data["request_id"], res["X-Request-ID"])

    def test_points_geometry_on_request(self):
        """geometry=points returns the decoded line, for clients that want it ready to plot."""
        with fake_osrm(35.0, -100.0, -85.0, 850):
            res = self.client.get(ROUTE_URL, {"from": "West Town, OK", "to": "East Town, TN",
                                              "geometry": "points"})
        route = res.json()["route"]
        self.assertEqual(route["format"], "points")
        self.assertEqual(len(route["points"]), 2001)
        self.assertNotIn("polyline", route)

    def test_distances_fall_back_without_annotations(self):
        """An OSRM that sends no annotations still works, and says so in meta."""
        with fake_osrm(35.0, -100.0, -85.0, 850, annotate=False):
            res = self.client.get(ROUTE_URL, {"from": "West Town, OK", "to": "East Town, TN"})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["meta"]["distance_source"], "estimated")

    def test_route_is_cached(self):
        """The second identical request doesn't call OSRM again."""
        with fake_osrm(35.0, -100.0, -85.0, 850) as osrm:
            self.client.get(ROUTE_URL, {"from": "West Town, OK", "to": "East Town, TN"})
            res = self.client.get(ROUTE_URL, {"from": "West Town, OK", "to": "East Town, TN"})
        self.assertEqual(osrm.call_count, 1)
        self.assertTrue(res.json()["meta"]["route_cached"])

    def test_cached_route_needs_no_decoding(self):
        """A warm request skips the polyline decode entirely unless points were asked for."""
        with fake_osrm(35.0, -100.0, -85.0, 850):
            self.client.get(ROUTE_URL, {"from": "West Town, OK", "to": "East Town, TN"})
            with mock.patch("planner.services.trip.decode_polyline") as decode:
                res = self.client.get(ROUTE_URL, {"from": "West Town, OK", "to": "East Town, TN"})
        self.assertEqual(res.status_code, 200, res.content)
        decode.assert_not_called()

    def test_post_and_polyline_geometry(self):
        """POST with a JSON body works too, and geometry=polyline returns the encoded string."""
        with fake_osrm(35.0, -100.0, -85.0, 850):
            res = self.client.post(ROUTE_URL, {"from": "West Town, Oklahoma", "to": "35,-85",
                                               "geometry": "polyline"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        route = res.json()["route"]
        self.assertIn("polyline", route)
        self.assertNotIn("points", route)

    def test_upstream_failure_is_503(self):
        """If OSRM is down, the client gets a clean 503, not a crash."""
        with mock.patch("planner.services.routing.get_json", side_effect=UpstreamUnavailable()):
            res = self.client.get(ROUTE_URL, {"from": "West Town, OK", "to": "East Town, TN"})
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["error"]["code"], "upstream_unavailable")

    def test_saint_is_normalized(self):
        """'Saint Louis' and 'St. Louis' resolve to the same city row."""
        from .services.geocoding import geocode
        self.assertEqual(geocode("Saint Louis, MO")["lat"], geocode("St. Louis, Missouri")["lat"])

    def test_city_lookups_stay_on_the_calling_thread(self):
        """
        The city table is read inline, never from a worker thread.

        A thread opens its own database connection, outside the caller's transaction, so it
        would not see rows the caller has yet to commit.
        """
        import threading
        from .services import geocoding
        seen = set()
        original = geocoding._lookup_city

        def record(query):
            """Note which thread each city lookup ran on."""
            seen.add(threading.current_thread().name)
            return original(query)

        with fake_osrm(35.0, -100.0, -85.0, 850), \
                mock.patch.object(geocoding, "_lookup_city", side_effect=record):
            res = self.client.get(ROUTE_URL, {"from": "West Town, OK", "to": "East Town, TN"})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(seen, {threading.current_thread().name})


class GeocodeConcurrencyTests(APITestBase):
    """Nominatim lookups touch no database, so those are the ones worth overlapping."""

    def test_two_address_lookups_overlap(self):
        """
        Two places needing Nominatim are fetched at the same time, not one after the other.

        Both stand-ins wait at a barrier, so the request can only finish if the two lookups
        are genuinely in flight together. Run in sequence, the barrier times out.
        """
        import threading
        from .services import geocoding

        barrier = threading.Barrier(2, timeout=10)

        def fake_search(url, params=None, headers=None, ok_statuses=(200,)):
            """Stand in for Nominatim, held until the other lookup arrives too."""
            barrier.wait()
            return 200, [{"lat": "35.0", "lon": "-99.0", "display_name": params["q"]}]

        with fake_osrm(35.0, -100.0, -85.0, 850), \
                mock.patch.object(geocoding, "get_json", side_effect=fake_search), \
                mock.patch.object(geocoding, "_wait_for_nominatim_slot"):
            res = self.client.get(ROUTE_URL, {"from": "1 Main Street", "to": "2 Oak Avenue"})

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(barrier.n_waiting, 0)
        self.assertFalse(barrier.broken, "the two lookups did not overlap")

    def test_single_address_lookup_needs_no_thread(self):
        """One address and one city: no pool is started for a single lookup."""
        import threading
        from .services import geocoding

        threads = set()

        def fake_search(url, params=None, headers=None, ok_statuses=(200,)):
            """Stand in for Nominatim, recording which thread asked."""
            threads.add(threading.current_thread().name)
            return 200, [{"lat": "35.0", "lon": "-99.0", "display_name": params["q"]}]

        with fake_osrm(35.0, -100.0, -85.0, 850), \
                mock.patch.object(geocoding, "get_json", side_effect=fake_search), \
                mock.patch.object(geocoding, "_wait_for_nominatim_slot"):
            res = self.client.get(ROUTE_URL, {"from": "West Town, OK", "to": "2 Oak Avenue"})

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(threads, {threading.current_thread().name})


class CacheOutageTests(APITestBase):
    """A cache outage must cost speed, never availability."""

    def test_route_still_plans_without_the_cache(self):
        """Every cache call failing still yields a full, correct plan."""
        with fake_osrm(35.0, -100.0, -85.0, 850), broken_cache():
            res = self.client.get(ROUTE_URL, {"from": "West Town, OK", "to": "East Town, TN"})
        self.assertEqual(res.status_code, 200, res.content)
        data = res.json()
        self.assertGreaterEqual(data["summary"]["fuel_stops"], 1)
        self.assertFalse(data["meta"]["route_cached"])
        # The response says plainly that it was served in degraded mode.
        self.assertTrue(data["meta"]["cache_degraded"])

    def test_station_index_survives_an_outage(self):
        """The index comes from PostgreSQL, so losing the cache must not empty it."""
        from .services.stations import get_station_index
        before = len(get_station_index().stations)
        with broken_cache():
            self.assertEqual(len(get_station_index().stations), before)

    def test_health_reports_degraded_but_stays_up(self):
        """Cache down, database fine: 200 and "degraded", because the API still works."""
        with broken_cache():
            res = APIClient().get("/api/v1/health/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "degraded")
        self.assertEqual(res.json()["checks"]["cache"], "error")

    def test_health_is_503_when_the_database_is_down(self):
        """No database means nothing can be planned, so the probe must fail."""
        with mock.patch("planner.views.connection") as conn:
            conn.cursor.side_effect = RuntimeError("database is down")
            res = APIClient().get("/api/v1/health/")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["checks"]["database"], "error")


class PriceFileTests(TestCase):
    """Reading the supplied CSV: one entry per truck stop, placed on the map."""

    CSV = (
        "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
        "7,WOODSHED OF BIG CABIN,\"I-44, EXIT 283\",Big Cabin,OK,307,3.00733333\n"
        "20,PILOT TRAVEL CENTER #1243,\"I-8\",Gila Bend,AZ,930,3.899\n"
        "20,PILOT #1243,\"I-8\",Gila Bend,AZ,930,3.499\n"          # same site, two listings
        "31,CIRCLE K,\"HWY 401\",Mississauga,ON,100,3.210\n"       # Canada: not a US trip
        "44,BROKEN PUMP,\"I-10\",Big Cabin,OK,307,not-a-number\n"  # unusable price
        "55,BROOKPARK FUEL,\"I-71\",Brookpark,OH,420,3.150\n"      # city needs an alias
        "66,WENTWORTH FUEL,\"I-95\",Port Wentworth,GA,421,3.250\n"  # city needs coordinates
        "77,NOWHERE FUEL,\"I-1\",Atlantis,XX,999,3.000\n"          # bad state
        "88,LOST FUEL,\"I-2\",Nosuchplace,OK,998,3.000\n"          # placeable nowhere
    )

    CITIES = [
        {"name": "big cabin", "state": "ok", "lat": 36.53, "lon": -95.22},
        {"name": "gila bend", "state": "az", "lat": 32.95, "lon": -112.72},
        {"name": "brook park", "state": "oh", "lat": 41.40, "lon": -81.81},
    ]
    ALIASES = {"brookpark|oh": "brook park"}
    COORDS = {"port wentworth|ga": [32.1491, -81.1632]}

    def setUp(self):
        """Write the sample CSV to a temp file for each test."""
        import tempfile
        self.path = Path(tempfile.mkdtemp()) / "prices.csv"
        self.path.write_text(self.CSV, encoding="utf-8")

    def test_one_entry_per_truck_stop(self):
        """Repeat listings of one OPIS id collapse into a single station."""
        from .data_loader import read_prices
        stations = {s["external_key"]: s for s in read_prices(self.path)}
        self.assertIn("opis:20", stations)
        self.assertEqual(len([k for k in stations if k == "opis:20"]), 1)

    def test_duplicate_listings_keep_the_lowest_price_and_longest_name(self):
        """
        Of two listings for one site, the cheaper price and the fuller name are kept.

        The file gives no date to tell the two apart, and this is a planner for cheap fuel, so
        the lower of two real quoted prices is the one worth showing.
        """
        from .data_loader import read_prices
        station = {s["external_key"]: s for s in read_prices(self.path)}["opis:20"]
        self.assertEqual(station["price"], Decimal("3.499"))
        self.assertEqual(station["name"], "PILOT TRAVEL CENTER #1243")

    def test_canadian_rows_are_kept(self):
        """
        Truck stops outside the USA stay in. Every price in the file is loaded.

        A route near the border can legitimately pass one, and the loader is not the place to
        decide a priced station does not count.
        """
        from .data_loader import read_prices
        keys = {s["external_key"] for s in read_prices(self.path)}
        self.assertIn("opis:31", keys)      # Mississauga, ON

    def test_only_unusable_rows_are_skipped(self):
        """A row with no state or an unreadable price has nothing to load."""
        from .data_loader import read_prices
        keys = {s["external_key"] for s in read_prices(self.path)}
        self.assertNotIn("opis:44", keys)   # price "not-a-number"

    def test_supplement_places_cities_the_table_lacks(self):
        """An alias and an explicit coordinate pair each rescue a station that would be lost."""
        from .data_loader import locate_stations, read_prices
        located, unplaced = locate_stations(
            read_prices(self.path), self.CITIES, self.ALIASES, self.COORDS)
        by_key = {s["external_key"]: s for s in located}
        self.assertEqual((by_key["opis:55"]["lat"], by_key["opis:55"]["lon"]), (41.40, -81.81))
        self.assertEqual((by_key["opis:66"]["lat"], by_key["opis:66"]["lon"]), (32.1491, -81.1632))
        # Anything no source can place is reported by name, not dropped in silence.
        self.assertEqual({s["external_key"] for s in unplaced},
                         {"opis:31", "opis:77", "opis:88"})

    def test_missing_file_is_a_clear_error(self):
        """A missing price file says so, rather than failing somewhere deeper."""
        from .data_loader import DataFileError, read_prices
        with self.assertRaises(DataFileError):
            read_prices(self.path.parent / "absent.csv")

    def test_missing_column_is_a_clear_error(self):
        """A CSV without the columns we need is rejected by name."""
        from .data_loader import DataFileError, read_prices
        bad = self.path.parent / "bad.csv"
        bad.write_text("City,State\nBig Cabin,OK\n", encoding="utf-8")
        with self.assertRaises(DataFileError) as ctx:
            read_prices(bad)
        self.assertIn("Retail Price", str(ctx.exception))


class RealPriceFileTests(TestCase):
    """The actual supplied CSV must load completely, with nothing quietly discarded."""

    def test_every_truck_stop_is_placed(self):
        """
        Not one priced truck stop in the file is dropped, anywhere.

        Guards the whole chain: if a future price file names a city nothing can locate, this
        fails with the city's name rather than letting the station disappear.
        """
        from .data_loader import locate_stations, read_cities, read_prices, read_supplement
        aliases, coordinates = read_supplement()
        stations = read_prices()
        located, unplaced = locate_stations(stations, read_cities(), aliases, coordinates)
        self.assertEqual(
            unplaced, [], f"unplaced: {sorted({(s['city'], s['state']) for s in unplaced})}")
        self.assertEqual(len(located), len(stations))
        self.assertEqual(len(located), 6738)

    def test_canadian_truck_stops_are_included(self):
        """The 112 stations in Canadian provinces are loaded along with the rest."""
        from .data_loader import read_prices
        provinces = {"AB", "BC", "MB", "NB", "NS", "ON", "QC", "SK", "YT"}
        canadian = [s for s in read_prices() if s["state"] in provinces]
        self.assertEqual(len(canadian), 112)

    def test_keys_are_the_files_own_identifier(self):
        """Stations are keyed on the OPIS Truckstop ID, which is stable across reloads."""
        from .data_loader import read_prices
        stations = read_prices()
        self.assertTrue(all(s["external_key"].startswith("opis:") for s in stations))
        self.assertEqual(len({s["external_key"] for s in stations}), len(stations))


class StationIdentityTests(APITestBase):
    """A station_id handed to a client must still mean the same station after a reload."""

    def test_ids_survive_a_price_reload(self):
        """Reloading the data updates rows in place instead of renumbering the table."""
        from .data_loader import load

        before = dict(FuelStation.objects.values_list("external_key", "id"))
        rows = [{**s, "price": s["price"] + Decimal("0.100")}
                for s in FuelStation.objects.values(
                    "name", "city", "state", "price", "external_key")]
        with mock.patch("planner.data_loader.read_prices", return_value=rows), \
                mock.patch("planner.data_loader.read_cities", return_value=[]), \
                mock.patch("planner.data_loader.locate_stations",
                           side_effect=lambda s, c, a=None, g=None: (
                               [{**r, "lat": 35.01, "lon": -99.0} for r in s], [])):
            load(City, FuelStation)

        after = dict(FuelStation.objects.values_list("external_key", "id"))
        self.assertEqual(before, after)
        self.assertEqual(FuelStation.objects.count(), len(before))


class HealthAndThrottleTests(APITestBase):
    """Public health check and rate limiting."""

    def test_health(self):
        """Health is public and reports both dependencies."""
        res = APIClient().get("/api/v1/health/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "ok", "checks": {"database": "ok", "cache": "ok"}})

    def test_health_has_its_own_generous_limit(self):
        """
        Health is not throttled on the shared burst scope.

        A fleet of probes behind one proxy address would otherwise rate-limit itself, and a
        balancer reads the resulting 429 as "unhealthy" and pulls a working instance.
        """
        from .throttling import APIKeyBurstThrottle
        with mock.patch.object(APIKeyBurstThrottle, "rate", "1/minute", create=True):
            codes = [APIClient().get("/api/v1/health/").status_code for _ in range(5)]
        self.assertEqual(codes, [200] * 5)

    def test_rate_limit(self):
        """Over the limit -> 429 with a Retry-After header."""
        with mock.patch.object(HealthThrottle, "rate", "2/minute", create=True):
            client = APIClient()
            codes = [client.get("/api/v1/health/").status_code for _ in range(3)]
            last = client.get("/api/v1/health/")
        self.assertEqual(codes, [200, 200, 429])
        self.assertEqual(last.json()["error"]["code"], "rate_limited")
        self.assertIn("Retry-After", last)
