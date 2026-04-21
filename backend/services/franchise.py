"""Franchise mapping — single source of truth for historical team code groupings."""

# Maps any team code to all codes that belong to the same franchise.
# Use get_franchise_codes(code) to get the list.
FRANCHISE_MAP = {
    # Athletics: Philadelphia (PHA 1901-1954) → Kansas City (KC1 1955-1967) →
    # Oakland (OAK 1968-2024) → Athletics (ATH 2025+, post-Oakland)
    "ATH": ["ATH", "OAK", "KC1", "PHA"],
    "OAK": ["ATH", "OAK", "KC1", "PHA"],
    "KC1": ["ATH", "OAK", "KC1", "PHA"],
    "PHA": ["ATH", "OAK", "KC1", "PHA"],
    "WAS": ["WAS", "MON"],               # Nationals ← Expos
    "MON": ["WAS", "MON"],
    "MIA": ["MIA", "FLO"],               # Marlins (Florida → Miami)
    "FLO": ["MIA", "FLO"],
    # Angels: LAA (1961-1964) → California (CAL 1965-1996) → Anaheim (ANA 1997+)
    "ANA": ["ANA", "CAL", "LAA"],
    "CAL": ["ANA", "CAL", "LAA"],
    "LAA": ["ANA", "CAL", "LAA"],
    "TBA": ["TBA"],                       # Rays
    "BAL": ["BAL", "MLA", "SLA"],        # Orioles ← St. Louis Browns ← Milwaukee
    "MLA": ["BAL", "MLA", "SLA"],
    "SLA": ["BAL", "MLA", "SLA"],
    "MIN": ["MIN", "WS1"],               # Twins ← Washington Senators (original)
    "WS1": ["MIN", "WS1"],
    "TEX": ["TEX", "WS2"],               # Rangers ← Washington Senators (expansion)
    "WS2": ["TEX", "WS2"],
    "ATL": ["ATL", "BSN", "MLN"],        # Braves: Boston → Milwaukee → Atlanta
    "BSN": ["ATL", "BSN", "MLN"],
    "MLN": ["ATL", "BSN", "MLN"],
    "SFN": ["SFN", "NY1"],               # Giants: New York → San Francisco
    "NY1": ["SFN", "NY1"],
    "LAN": ["LAN", "BRO"],               # Dodgers: Brooklyn → Los Angeles
    "BRO": ["LAN", "BRO"],
    # Brewers: Seattle Pilots (SE1 1969 — one season) → Milwaukee (MIL 1970+)
    "MIL": ["MIL", "SE1"],
    "SE1": ["MIL", "SE1"],
    # Yankees: Baltimore Orioles AL (BLA 1901-1902) → NY Highlanders/Yankees (NYA 1903+)
    "NYA": ["NYA", "BLA"],
    "BLA": ["NYA", "BLA"],
}

# Display names for franchise references (uses current city/name)
FRANCHISE_DISPLAY = {
    "ATH": "Athletics", "OAK": "Athletics", "KC1": "Athletics", "PHA": "Athletics",
    "WAS": "Nationals", "MON": "Nationals",
    "MIA": "Marlins", "FLO": "Marlins",
    "ANA": "Angels", "CAL": "Angels", "LAA": "Angels",
    "TBA": "Rays",
    "BAL": "Orioles", "MLA": "Orioles", "SLA": "Orioles",
    "MIN": "Twins", "WS1": "Twins",
    "TEX": "Rangers", "WS2": "Rangers",
    "ATL": "Braves", "BSN": "Braves", "MLN": "Braves",
    "SFN": "Giants", "NY1": "Giants",
    "LAN": "Dodgers", "BRO": "Dodgers",
    "MIL": "Brewers", "SE1": "Brewers",
    "NYA": "Yankees", "BLA": "Yankees",
    "BOS": "Red Sox", "CHA": "White Sox",
    "CHN": "Cubs", "CIN": "Reds", "CLE": "Guardians",
    "COL": "Rockies", "DET": "Tigers", "HOU": "Astros",
    "KCA": "Royals", "NYN": "Mets",
    "PHI": "Phillies", "PIT": "Pirates", "SDN": "Padres",
    "SEA": "Mariners", "SLN": "Cardinals", "TOR": "Blue Jays",
    "ARI": "Diamondbacks",
}

# Canonical franchise code — the "current" code for each franchise. Used by
# build_records to consolidate records under one entry per franchise rather
# than one per historical city/name. Keyed by any member code.
FRANCHISE_CANONICAL = {
    "ATH": "ATH", "OAK": "ATH", "KC1": "ATH", "PHA": "ATH",
    "WAS": "WAS", "MON": "WAS",
    "MIA": "MIA", "FLO": "MIA",
    "ANA": "ANA", "CAL": "ANA", "LAA": "ANA",
    "BAL": "BAL", "MLA": "BAL", "SLA": "BAL",
    "MIN": "MIN", "WS1": "MIN",
    "TEX": "TEX", "WS2": "TEX",
    "ATL": "ATL", "BSN": "ATL", "MLN": "ATL",
    "SFN": "SFN", "NY1": "SFN",
    "LAN": "LAN", "BRO": "LAN",
    "MIL": "MIL", "SE1": "MIL",
    "NYA": "NYA", "BLA": "NYA",
}


def get_franchise_codes(code: str) -> list[str]:
    """Get all historical team codes for a franchise."""
    return FRANCHISE_MAP.get(code, [code])


def get_franchise_name(code: str) -> str:
    """Get the current franchise display name for any team code."""
    return FRANCHISE_DISPLAY.get(code, code)


def get_canonical_code(code: str) -> str:
    """Get the canonical (current-city) team code for any historical team code.

    For teams that never moved, returns the input. For relocated franchises,
    returns the single current-city code (e.g. "PHA" → "OAK", "MON" → "WAS").
    """
    return FRANCHISE_CANONICAL.get(code, code)
