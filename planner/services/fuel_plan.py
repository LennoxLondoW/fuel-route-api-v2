"""
Cost-minimising refuelling plan (greedy "next cheaper station" strategy).

Idea, at each station we stop at:
  - If a station clearly cheaper than this one is reachable, buy just enough fuel to get there.
  - Otherwise this is the best price around: fill the tank, and drive to the cheapest station
    in range (preferring ones at least MIN_HOP miles away, to avoid many tiny stops).

That rule is the textbook greedy for this problem, and an exact dynamic program over the same
candidate list confirms it reaches the true optimum once MIN_SAVING and MIN_HOP are set to
zero. Those two knobs trade a fraction of a percent of cost for a shorter, more practical
stop list.

Distances
---------
Stations sit up to CORRIDOR_MILES off the route, so stopping at one is a detour: `offset`
miles to leave the route and `offset` miles to rejoin it. Both are charged, to the leg that
actually incurs them, and they count towards fuel used and towards whether the next station
is reachable at all. A station we pass without buying costs no detour.

Every planned leg ends with at least RESERVE_MILES of range still in the tank, so the plan
does not depend on arriving at a pump with a dry tank.

Money
-----
Prices are Decimal and every dollar figure is quantized to the cent with ROUND_HALF_UP, so
totals do not inherit binary floating-point drift. Distances and gallons stay float.

Billing rules (same as the original prototype):
  - The truck leaves with a full tank. That tank is billed at the first purchase's price
    (or the cheapest price on the route if no stop is needed).
  - Fuel still in the tank at the destination is credited back. The credit uses the running
    average price of what is actually in the tank, not whatever the last station charged.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from ..exceptions import NoStationInRange

CENT = Decimal("0.01")
ZERO = Decimal("0")

# Below this many miles of range, a "purchase" is treated as passing the station by.
PURCHASE_EPSILON = 0.01


def _money(value):
    """Round a Decimal dollar amount to the cent, half up (not banker's rounding)."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Stop:
    """One station on the plan. `buy_miles` of 0 means we drove past without buying."""

    station: object            # a stations.Candidate
    arrive_miles: float        # range left when arriving here
    buy_miles: float = 0.0     # range bought here
    gallons: float = 0.0
    cost: Decimal = ZERO

    @property
    def is_purchase(self):
        """True if fuel was actually bought here (and therefore a detour was driven)."""
        return self.buy_miles > PURCHASE_EPSILON


@dataclass
class FuelPlan:
    """The finished plan: where to buy, what it costs, and how far the truck really drives."""

    stops: list = field(default_factory=list)   # only stops where fuel is actually bought
    start_gallons: float = 0.0
    start_price: Decimal | None = None
    start_price_source: str = ""
    start_cost: Decimal | None = None
    leftover_gallons: float = 0.0
    leftover_credit: Decimal = ZERO
    total_cost: Decimal | None = None
    detour_miles: float = 0.0      # extra miles driven to reach the stops and rejoin
    driven_miles: float = 0.0      # route distance plus detour_miles


class _Tank:
    """
    Tracks both how much range is in the tank and what was paid for it.

    Fuel is mixed, so a burn removes value in proportion to the range it uses. That makes the
    credit for fuel left at the destination the money actually spent on it.
    """

    def __init__(self, miles, gallons_price=None, mpg=10.0):
        """Start with `miles` of range; its value is set later by `price_initial_fill`."""
        self.miles = float(miles)
        self.value = ZERO
        self.mpg = float(mpg)
        if gallons_price is not None:
            self.price_initial_fill(gallons_price)

    def price_initial_fill(self, price):
        """Bill whatever is currently in the tank at `price` per gallon."""
        self.value = Decimal(repr(self.miles / self.mpg)) * price

    def buy(self, miles, price):
        """Add `miles` of range bought at `price` per gallon. Returns the cost."""
        if miles <= 0:
            return ZERO
        cost = Decimal(repr(miles / self.mpg)) * price
        self.miles += miles
        self.value += cost
        return cost

    def burn(self, miles):
        """Consume `miles` of range, removing its share of the tank's value."""
        if miles <= 0 or self.miles <= 0:
            return
        used = min(miles, self.miles)
        self.value -= self.value * Decimal(repr(used)) / Decimal(repr(self.miles))
        self.miles -= used
        if self.miles <= 1e-9:
            self.miles = 0.0
            self.value = ZERO


def _cheapest(stations):
    """Lowest price; on a tie, the one furthest along the route."""
    return min(stations, key=lambda s: (s.price, -s.mile))


def _range_phrase(range_miles, reserve):
    """
    How far the planner will go on one tank, spelled out for an error message.

    With no reserve this is simply the vehicle's range, so the figure a caller sees matches
    the range they were told to expect. With a reserve it says where the shortfall comes from
    rather than quoting a number that looks like the range is wrong.
    """
    if reserve <= 0:
        return f"{round(range_miles)} miles"
    return (f"{round(range_miles - reserve)} miles "
            f"(a {round(range_miles)}-mile range less a {round(reserve)}-mile reserve)")


def plan_fuel_stops(candidates, total_miles, *, range_miles, mpg, min_saving, min_hop,
                    reserve_miles=0.0, fallback_price=None):
    """
    candidates:     stations near the route, sorted by .mile
    total_miles:    trip length along the route (detours are added on top)
    reserve_miles:  range still in the tank when arriving anywhere
    fallback_price: price used for the starting tank if the route has no stations at all
    """
    min_saving = Decimal(str(min_saving))
    reserve = float(reserve_miles)
    range_miles = float(range_miles)
    # The longest leg we are willing to plan: a full tank minus the reserve.
    usable = range_miles - reserve
    if usable <= 0:
        raise ValueError("reserve_miles must be smaller than range_miles")

    stops = []
    fuel = range_miles      # start with a full tank
    current = None          # station we are at (None = still at the start)
    tank = _Tank(range_miles, mpg=mpg)
    leftover_miles = 0.0

    def leg_to(frm_stop, to_station):
        """
        Miles driven from `frm_stop` (None = the start) to `to_station`.

        Includes the detour out of the station we are leaving, if we bought there, and the
        detour into the one we are heading for. Entering is charged optimistically: if it
        turns out we buy nothing there, the caller drops it, which can only shorten the leg.
        """
        if frm_stop is None:
            return to_station.mile + to_station.offset
        exit_detour = frm_stop.station.offset if frm_stop.is_purchase else 0.0
        return (to_station.mile - frm_stop.station.mile) + exit_detour + to_station.offset

    def leg_to_finish(frm_stop):
        """Miles from `frm_stop` (None = the start) to the destination."""
        if frm_stop is None:
            return total_miles
        exit_detour = frm_stop.station.offset if frm_stop.is_purchase else 0.0
        return (total_miles - frm_stop.station.mile) + exit_detour

    while True:
        # --- At the start: choose the first stop. ---
        if current is None:
            if total_miles + reserve <= fuel:
                break  # the whole trip fits in one tank, reserve included

            reachable = [s for s in candidates if s.mile + s.offset <= usable]
            if not reachable:
                raise NoStationInRange(
                    "No fuel station within "
                    f"{_range_phrase(range_miles, reserve)} of the start.")
            not_too_close = [s for s in reachable if s.mile >= min_hop]
            first = _cheapest(not_too_close or reachable)

            driven = first.mile + first.offset
            fuel = range_miles - driven
            # The starting tank is billed at this stop's price (see the module docstring).
            tank.price_initial_fill(first.price)
            tank.burn(driven)
            current = first
            stops.append(Stop(current, arrive_miles=fuel))
            continue

        stop = stops[-1]
        remaining = leg_to_finish(stop)

        # Stations reachable from here on a full tank, keeping the reserve.
        window = [s for s in candidates
                  if s.mile > current.mile and leg_to(stop, s) <= usable]
        # First one that is meaningfully cheaper than here.
        cheaper = next((s for s in window if s.price < current.price - min_saving), None)

        # --- Can we finish from here (and there is no cheaper station before the end)? ---
        if remaining <= usable and (cheaper is None or total_miles <= cheaper.mile):
            stop.buy_miles = max(0.0, remaining + reserve - fuel)
            if not stop.is_purchase:
                stop.buy_miles = 0.0
                remaining = leg_to_finish(stop)   # no purchase here, so no detour either
            stop.cost = tank.buy(stop.buy_miles, current.price)
            fuel += stop.buy_miles
            tank.burn(remaining)
            fuel -= remaining
            leftover_miles = max(0.0, fuel)
            break

        if cheaper is not None:
            # Buy only enough to reach the cheaper station, arriving with the reserve.
            nxt = cheaper
            distance = leg_to(stop, nxt)
            stop.buy_miles = max(0.0, distance + reserve - fuel)
        else:
            # This is the cheapest around: fill up and go to the best-priced station in range.
            if not window:
                raise NoStationInRange(
                    "No fuel station within "
                    f"{_range_phrase(range_miles, reserve)} after mile "
                    f"{round(current.mile)}.")
            far_enough = [s for s in window if s.mile - current.mile >= min_hop]
            nxt = _cheapest(far_enough or window)
            stop.buy_miles = max(0.0, range_miles - fuel)
            distance = leg_to(stop, nxt)

        if not stop.is_purchase:
            # Driving past, not stopping: drop both this station's detours from the leg.
            stop.buy_miles = 0.0
            distance = leg_to(stop, nxt)

        stop.cost = tank.buy(stop.buy_miles, current.price)
        fuel += stop.buy_miles
        tank.burn(distance)
        fuel -= distance
        current = nxt
        stops.append(Stop(current, arrive_miles=fuel))

    # --- Costs ---
    for s in stops:
        s.gallons = s.buy_miles / mpg
        s.cost = _money(s.cost)

    purchases = [s for s in stops if s.is_purchase]

    plan = FuelPlan()
    plan.stops = purchases
    plan.detour_miles = sum(s.station.detour_miles for s in purchases)
    plan.driven_miles = total_miles + plan.detour_miles
    plan.leftover_gallons = leftover_miles / mpg
    plan.leftover_credit = _money(tank.value)

    if purchases:
        plan.start_gallons = range_miles / mpg
        plan.start_price = purchases[0].station.price
        plan.start_price_source = "first_stop"
    else:
        # No purchase anywhere: only the fuel actually burned is billed.
        plan.start_gallons = plan.driven_miles / mpg
        plan.leftover_credit = ZERO
        if candidates:
            plan.start_price = min(s.price for s in candidates)
            plan.start_price_source = "cheapest_on_route"
        elif fallback_price is not None:
            plan.start_price = Decimal(str(fallback_price))
            plan.start_price_source = "national_average"
        else:
            plan.start_price_source = "unknown"

    if plan.start_price is not None:
        plan.start_cost = _money(Decimal(repr(plan.start_gallons)) * plan.start_price)
        plan.total_cost = _money(
            plan.start_cost - plan.leftover_credit + sum((s.cost for s in purchases), ZERO))

    return plan
