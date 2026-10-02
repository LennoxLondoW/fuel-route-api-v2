"""
Business logic, kept separate from Django views so it is easy to read and test:

    geocoding.py   "New York, NY"  ->  lat/lon     (offline city table, Nominatim fallback)
    routing.py     two points      ->  road route  (OSRM) + polyline decoder + route thinning
    stations.py    route           ->  fuel stations within N miles of it (in-memory grid index)
    fuel_plan.py   stations        ->  cheapest refuelling plan
    trip.py        glues the above together and builds the JSON response

    safe_cache.py  cache access that degrades to a miss instead of raising, because Redis
                   here is an accelerator and not a source of truth
"""
