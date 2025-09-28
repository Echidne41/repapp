# backend/app.py
import os, re
from flask import Flask, jsonify, request, send_from_directory
try:
    from flask_cors import CORS  # optional
except Exception:
    CORS = None

import requests
from shapely.geometry import shape, Point

# Import loader (works whether backend is a package or not)
try:
    from .loader import load_geoindex, load_floterials, load_votes, district_key_variants
except Exception:
    from loader import load_geoindex, load_floterials, load_votes, district_key_variants

app = Flask(__name__, static_folder=None)
if CORS:
    CORS(app)

# ---- County code maps ----
COUNTY_ABBR_2 = {
    "BE": "Belknap", "CA": "Carroll", "CH": "Cheshire", "CO": "Coos",
    "GR": "Grafton", "HI": "Hillsborough", "ME": "Merrimack",
    "RO": "Rockingham", "ST": "Strafford", "SU": "Sullivan",
}
COUNTY_2_TO_3 = {
    "BE": "BEL", "CA": "CAR", "CH": "CHE", "CO": "COO",
    "GR": "GRA", "HI": "HIL", "ME": "MER",
    "RO": "ROC", "ST": "STR", "SU": "SUL",
}
COUNTY_NAME_TO_3 = {v: COUNTY_2_TO_3[k2] for k2, v in COUNTY_ABBR_2.items()}

def code_to_district_name(s: str) -> str:
    """GR15 -> Grafton 15; also 'Grafton-15' -> 'Grafton 15'."""
    if not s: return s
    s = str(s).strip()
    m = re.fullmatch(r"([A-Z]{2})\s*-?\s*(\d+)", s)
    if m:
        return f"{COUNTY_ABBR_2.get(m.group(1), m.group(1))} {int(m.group(2))}"
    if "-" in s:
        left, right = s.split("-", 1)
        if right.strip().isdigit():
            return f"{left.strip()} {int(right.strip())}"
    return s

# ---- Load data (CSV + GeoJSON) ----
GEO = load_geoindex()                          # reads backend/data/nh_house_districts.json
BASE_TO_FLOTS, TOWN_TO_FLOTS = load_floterials()
REPS_BY_DIST, REP_INFO, ISSUES = load_votes()

# ---- Build polygon list for point-in-polygon (base districts) ----
DIST_SHAPES = []  # list[(shapely_geom, district_string)]
for feat in getattr(GEO, "items", []) or []:
    props = feat.get("properties") or {}
    geom = feat.get("geometry")
    district_name = (
        props.get("district") or props.get("DISTRICT") or
        props.get("Dist_Name") or props.get("DIST_NAME") or
        props.get("name") or props.get("NAME") or
        props.get("basehse22") or props.get("BaseHse22") or props.get("BASEHSE22")
    )
    if district_name and geom:
        try:
            DIST_SHAPES.append((shape(geom), str(district_name).strip()))
        except Exception:
            pass

def _sanitize_address(a: str) -> str:
    if not a: return a
    return re.sub(r",?\s*(apt|apartment|unit|ste|suite|#)\s*[^\s,]+", "", a, flags=re.I)

def _geocode_census(one_line: str):
    """Return (lat, lon, town_upper) or (None, None, '')."""
    if not one_line: return None, None, ""
    url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
    params = {"address": one_line, "benchmark": "Public_AR_Current", "format": "json"}
    try:
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
        matches = (data.get("result") or {}).get("addressMatches") or []
        if not matches: return None, None, ""
        m0 = matches[0]
        coords = m0.get("coordinates") or {}
        lon = coords.get("x"); lat = coords.get("y")
        comps = m0.get("addressComponents") or {}
        town = (comps.get("city") or comps.get("place") or comps.get("county") or "").upper().strip()
        if lat is None or lon is None: return None, None, ""
        return float(lat), float(lon), town
    except Exception:
        return None, None, ""

def _base_from_point(lat: float, lon: float):
    """Point-in-polygon on base districts; accept boundary touches."""
    if not DIST_SHAPES:
        return None
    p = Point(lon, lat)
    for geom, dname in DIST_SHAPES:
        try:
            if geom.contains(p) or geom.intersects(p):
                return dname
        except Exception:
            continue
    return None

def _three_letter_from_name_or_code(base_name: str, base_code: str) -> str | None:
    """
