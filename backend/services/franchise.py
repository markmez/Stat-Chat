"""Franchise mapping — single source of truth for historical team code groupings."""

# Maps any team code to all codes that belong to the same franchise.
# Use get_franchise_codes(code) to get the list.
FRANCHISE_MAP = {
    "ATH": ["ATH", "OAK", "PHA"],       # Athletics: Philadelphia → Kansas City → Oakland
    "OAK": ["ATH", "OAK", "PHA"],
    "PHA": ["ATH", "OAK", "PHA"],
    "WAS": ["WAS", "MON"],               # Nationals ← Expos
    "MON": ["WAS", "MON"],
    "MIA": ["MIA", "FLO"],               # Marlins (Florida → Miami)
    "FLO": ["MIA", "FLO"],
    "ANA": ["ANA", "CAL"],               # Angels (California → Anaheim)
    "CAL": ["ANA", "CAL"],
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
}

# Display names for franchise references (uses current city/name)
FRANCHISE_DISPLAY = {
    "ATH": "Athletics", "OAK": "Athletics", "PHA": "Athletics",
    "WAS": "Nationals", "MON": "Nationals",
    "MIA": "Marlins", "FLO": "Marlins",
    "ANA": "Angels", "CAL": "Angels",
    "TBA": "Rays",
    "BAL": "Orioles", "MLA": "Orioles", "SLA": "Orioles",
    "MIN": "Twins", "WS1": "Twins",
    "TEX": "Rangers", "WS2": "Rangers",
    "ATL": "Braves", "BSN": "Braves", "MLN": "Braves",
    "SFN": "Giants", "NY1": "Giants",
    "LAN": "Dodgers", "BRO": "Dodgers",
    "NYA": "Yankees", "BOS": "Red Sox", "CHA": "White Sox",
    "CHN": "Cubs", "CIN": "Reds", "CLE": "Guardians",
    "COL": "Rockies", "DET": "Tigers", "HOU": "Astros",
    "KCA": "Royals", "MIL": "Brewers", "NYN": "Mets",
    "PHI": "Phillies", "PIT": "Pirates", "SDN": "Padres",
    "SEA": "Mariners", "SLN": "Cardinals", "TOR": "Blue Jays",
    "ARI": "Diamondbacks",
}


def get_franchise_codes(code: str) -> list[str]:
    """Get all historical team codes for a franchise."""
    return FRANCHISE_MAP.get(code, [code])


def get_franchise_name(code: str) -> str:
    """Get the current franchise display name for any team code."""
    return FRANCHISE_DISPLAY.get(code, code)
