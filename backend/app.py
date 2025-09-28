# backend/app.py
import os, re
from flask import Flask, jsonify, request, send_from_directory
try:
    from flask_cors import CORS  # optional, helps if your frontend is on Vercel
except Exception:
    CORS = None

import requests
from shapely.geometry import shape, Point

from loader import (
    load_geoindex, load_floterials, load_votes, district_key_variants
)

app = Flask(__name__, static_folder=None)
if CORS:
    CORS(app)

# Map 2-letter county codes used in BaseHse22 to full county names
COUNTY_ABBR = {
    "BE": "Belknap", "CA": "Carroll", "CH": "Cheshire", "CO": "Coos",
    "GR": "Grafton", "HI": "Hillsborough", "ME": "Merrimack",
    "RO": "Rockingham", "ST": "Strafford", "SU": "Sullivan",
}

def code_to_district_name(s: str) -> str:
    """GR15 -> Grafton 15; also 'Grafton-15' -> 'Grafton 15'."""
    if not s: return s
    s = str(s).strip()
    m = re.fullmatch(r"([A-Z]{2})\s*-?\s*(\d+)", s)
    if m:
        county = COUNTY_ABBR.get(m.group(1), m.group(1))
        return f"{county} {int(m.group(2))}"
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
DIST_SHAPES = []  # list of (shapely_geom, district_string)
for feat in getattr(GEO, "items", []) or []:
    props = feat.get("properties") or {}
    geom = feat.get("geometry")
    # Try multiple property names seen in NH layers/exports
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
    # strip unit/apt/suite/# parts that confuse geocoders
    return re.sub(r",?\s*(apt|apartment|unit|ste|suite|#)\s*[^\s,]+", "", a, flags=re.I)

def _geocode_census(one_line: str):
    """Return (lat, lon) using Census Geocoder, or (None, None) on failure."""
    if not one_line: return None, None
    url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
    params = {"address": one_line, "benchmark": "Public_AR_Current", "format": "json"}
    try:
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
        matches = (data.get("result") or {}).get("addressMatches") or []
        if not matches:
            return None, None
        coords = matches[0].get("coordinates") or {}
        lon = coords.get("x"); lat = coords.get("y")
        return (lat, lon) if (lat is not None and lon is not None) else (None, None)
    except Exception:
        return None, None

def _base_from_point(lat: float, lon: float):
    """Point-in-polygon on base districts; accept points on boundaries."""
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

@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "counts": {
            "polygons": len(DIST_SHAPES),
            "base_to_flots": len(BASE_TO_FLOTS),
            "town_to_flots": len(TOWN_TO_FLOTS),
            "reps": len(REP_INFO),
            "issues": len(ISSUES),
        }
    })

@app.route("/lookup")
def lookup():
    addr = request.args.get("address", "").strip()
    lat = request.args.get("lat")
    lon = request.args.get("lon")
    town_upper = request.args.get("town", "")

    # ...your geocode step (or keep lat/lon handling)...

    # base found from point-in-polygon (likely 'GR15'):
    base_code = _base_from_point(float(lat), float(lon)) if lat and lon else None
    base_name = code_to_district_name(base_code)  # 'Grafton 15'

    rep_ids = []
    # try human-readable name first (matches your CSV), then the raw code
    for key in (district_key_variants(base_name) if base_name else []):
        rep_ids.extend(REPS_BY_DIST.get(key, []))
    for key in (district_key_variants(base_code) if base_code else []):
        rep_ids.extend(REPS_BY_DIST.get(key, []))

    # add floterials mapped by either key (in case your CSV uses names)
    flots = set()
    if base_name:
        flots.update(BASE_TO_FLOTS.get(base_name, []))
    if base_code:
        flots.update(BASE_TO_FLOTS.get(base_code, []))
    for f in sorted(list(flots)):
        for key in district_key_variants(f):
            rep_ids.extend(REPS_BY_DIST.get(key, []))

    # de-dup keep order
    seen, ordered = set(), []
    for r in rep_ids:
        if r not in seen:
            ordered.append(r); seen.add(r)
    reps = [REP_INFO[r] for r in ordered]

    return jsonify({
        "query": {"address": addr, "lat": lat, "lon": lon, "town": town_upper},
        "base_code": base_code,
        "base_district": base_name,    # human readable for the UI
        "floterials": sorted(list(flots)),
        "issues": ISSUES,
        "vote_columns": [i["slug"] for i in ISSUES],
        "reps": reps
    })


# ---- Serve the simple web UI from /web ----
@app.route("/")
def root():
    return send_from_directory(os.path.join(os.path.dirname(__file__), "..", "web"), "index.html")

@app.route("/<path:path>")
def static_proxy(path):
    web_dir = os.path.join(os.path.dirname(__file__), "..", "web")
    return send_from_directory(web_dir, path)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
