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
# 2-letter -> full county name (matches polygon codes like GR15)
COUNTY_ABBR_2 = {
    "BE": "Belknap", "CA": "Carroll", "CH": "Cheshire", "CO": "Coos",
    "GR": "Grafton", "HI": "Hillsborough", "ME": "Merrimack",
    "RO": "Rockingham", "ST": "Strafford", "SU": "Sullivan",
}
# 2-letter -> 3-letter (matches your CSV like ROC 30, HIL 12)
COUNTY_2_TO_3 = {
    "BE": "BEL", "CA": "CAR", "CH": "CHE", "CO": "COO",
    "GR": "GRA", "HI": "HIL", "ME": "MER",
    "RO": "ROC", "ST": "STR", "SU": "SUL",
}
# full county name -> 3-letter
COUNTY_NAME_TO_3 = {v: k3 for (k2, v) in COUNTY_ABBR_2.items() for k3 in [COUNTY_2_TO_3[k2]]}

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
    """Return CSV-style code like 'GRA 15' if possible."""
    # from two-letter code (e.g., GR15)
    m = re.fullmatch(r"([A-Z]{2})\s*-?\s*(\d+)", base_code or "")
    if m and m.group(1) in COUNTY_2_TO_3:
        return f"{COUNTY_2_TO_3[m.group(1)]} {int(m.group(2))}"
    # from name (e.g., 'Grafton 15')
    m2 = re.fullmatch(r"([A-Za-z]+)\s+(\d+)", base_name or "")
    if m2:
        k3 = COUNTY_NAME_TO_3.get(m2.group(1))
        if k3:
            return f"{k3} {int(m2.group(2))}"
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
    raw_addr = (request.args.get("address") or "").strip()
    lat_s = request.args.get("lat"); lon_s = request.args.get("lon")

    # Parse direct lat/lon if provided
    latf = lonf = None
    if lat_s and lon_s:
        try:
            latf, lonf = float(lat_s), float(lon_s)
        except Exception:
            latf = lonf = None

    # Geocode if needed (try with NH fallback)
    town_upper = ""
    if latf is None or lonf is None:
        addr = _sanitize_address(raw_addr)
        latf, lonf, town_upper = _geocode_census(addr)
        if latf is None or lonf is None and " NH" not in addr.upper():
            latf, lonf, town_upper = _geocode_census(addr + ", NH")
        if latf is None or lonf is None:
            return jsonify({"error": "could not geocode"}), 422

    # Base district from polygons (may be like GR15)
    base_code = _base_from_point(latf, lonf)
    base_name = code_to_district_name(base_code)
    csv_code3 = _three_letter_from_name_or_code(base_name, base_code)  # e.g., 'GRA 15'

    # Build all candidate keys to match your CSV indexing
    candidate_keys = []
    for s in [base_name, base_code, csv_code3]:
        if s:
            candidate_keys.extend(district_key_variants(s))
    # also include no-space version of csv_code3 (e.g., 'GRA15')
    if csv_code3:
        candidate_keys.append(csv_code3.replace(" ", ""))

    # Collect reps from base + floterials (by name/code/CSV code)
    rep_ids = []
    for key in candidate_keys:
        rep_ids.extend(REPS_BY_DIST.get(key, []))

    flots = set()
    for s in [base_name, base_code, csv_code3]:
        if s:
            flots.update(BASE_TO_FLOTS.get(s, []))
    if town_upper:
        flots.update(TOWN_TO_FLOTS.get(town_upper, []))
    for f in sorted(list(flots)):
        for key in district_key_variants(f):
            rep_ids.extend(REPS_BY_DIST.get(key, []))

    # De-dup, keep order
    seen, ordered = set(), []
    for r in rep_ids:
        if r not in seen:
            ordered.append(r); seen.add(r)
    reps = [REP_INFO[r] for r in ordered]

    return jsonify({
        "query": {"address": raw_addr, "lat": latf, "lon": lonf, "town": town_upper},
        "base_code": base_code,
        "base_district": base_name,
        "floterials": sorted(list(flots)),
        "issues": ISSUES,
        "vote_columns": [i["slug"] for i in ISSUES],  # back-compat
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
