"""US state names and codes, used by the offline geocoder to read "City, State" inputs."""

# Full state name -> two-letter code (lowercase).
STATES = {
    "alaska": "ak", "alabama": "al", "arkansas": "ar", "arizona": "az", "california": "ca",
    "colorado": "co", "connecticut": "ct", "district of columbia": "dc", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "iowa": "ia", "idaho": "id",
    "illinois": "il", "indiana": "in", "kansas": "ks", "kentucky": "ky", "louisiana": "la",
    "massachusetts": "ma", "maryland": "md", "maine": "me", "michigan": "mi", "minnesota": "mn",
    "missouri": "mo", "mississippi": "ms", "montana": "mt", "north carolina": "nc",
    "north dakota": "nd", "nebraska": "ne", "new hampshire": "nh", "new jersey": "nj",
    "new mexico": "nm", "nevada": "nv", "new york": "ny", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "puerto rico": "pr", "rhode island": "ri",
    "south carolina": "sc", "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "virginia": "va", "vermont": "vt", "washington": "wa", "wisconsin": "wi",
    "west virginia": "wv", "wyoming": "wy",
}

# All valid two-letter codes, for quick "is this a state code?" checks.
STATE_CODES = set(STATES.values())
