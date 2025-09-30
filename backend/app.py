# backend/app.py
import os, re
from typing import Optional, List, Set
from flask import Flask, jsonify, request, send_from_directory
try:
    from flask_cors import CORS  # optional
except Exception:
    CORS = None

import requests
from shapely.geometry import shape, Point

# Import from loader (works whether "backend" is a package or not)
try:
    from .loader import load_geoindex, load_floterials, load_votes, district_key_variants
except Exception:
    from loader import load_geoindex, load_floterials, load_votes, district_key_variants

app = Flask(__name__, static_folder=None)
if CORS:
    CORS(app)

# ----------------------------
# County code maps / converters
# ----------------------------
# 2-letter -> full county name (used by polygon attributes like GR15)
COUNTY_ABBR_2 = {
    "BE": "Belknap", "CA": "Carroll", "CH": "Cheshire", "CO": "Coos",
    "GR": "Grafton", "HI": "Hillsborough", "ME": "Merrimack",
    "RO": "Rockingham", "ST": "Strafford", "SU": "Sullivan",
}
# 2-letter -> 3-letter (matches your CSV keys like ROC 30, HIL 12, GRA 15)
COUNTY_2_TO_3 = {
    "BE": "BEL", "CA": "CAR", "CH": "CHE", "CO": "COO",
    "GR": "GRA", "HI": "HIL", "ME": "MER",
    "RO": "ROC", "ST": "STR", "SU": "SUL",
}
# full county name -> 3-letter
COUNTY_NAME_TO_3 = {v: COUNTY_2_TO_3[k2] for k2, v in COUNTY_ABBR_2.items()}

def code_to_district_name(s: Optional[str]) -> Optional[str]:
    """GR15 -> Grafton 15; also 'Grafton-15' -> 'Grafton 15'."""
    if not s:
        return s
    s = str(s).strip()
    m = re.fullmatch(r"([A-Z]{2})\s*-?\s*(\d+)", s)
    if m:
        return f"{COUNTY_ABBR_2.get(m.group(1), m.group(1))} {int(m.group(2))}"
    if "-" in s:
        left, right = s.split("-", 1)
        if right.strip().isdigit():
            return f"{left.strip()} {int(right.strip())}"
    return s

def three_letter_from_name_or_code(base_name: Optional[str], base_code: Optional[str]) -> Optional[str]:
    """Return CSV-style 3-letter code 'GRA 15' given 'Grafton 15' or 'GR15'."""
    m = re.fullmatch(r"([A-Z]{2})\s*-?\s*(\d+)", (base_code or "").strip())
    if m and m.group(1) in COUNTY_2_TO_3:
        return f"{COUNTY_2_TO_3[m.group(1)]} {int(m.group(2))}"
    m2 = re.fullmatch(r"([A-Za-z]+)\s+(\d+)", (base_name or "").strip())
    if m2:
        k3 = COUNTY_NAME_TO_3.get(m2.group(1))
        if k3:
            return f"{k3} {int(m2.group(2))}"
    return None

def variant_keys(*vals: Optional[str]) -> List[str]:
    """
    Generate aggressive lookup variants for a district string:
    - raw, uppercased/compacted variants from district_key_variants
    - hyphen <-> space toggles
    - no-hyphen/no-space uppercase compact forms (e.g., 'GRA 15' -> 'GRA15')
    """
    keys: Set[str] = set()
    for s in vals:
        if not s:
            continue
        s = str(s).strip()
        for k in district_key_variants(s):
            keys.add(k)
        if " " in s:
            for k in district_key_variants(s.replace(" ", "-")):
                keys.add(k)
        if "-" in s:
            for k in district_key_variants(s.replace("-", " ")):
                keys.add(k)
    more = set()
    for k in keys:
        more.add(re.sub(r"[\s-]+", "", k.upper()))
    keys |= more
    return list(keys)

# ----------------------------
# Load data (CSV + GeoJSON)
# ----------------------------
GEO = load_geoindex()                          # reads backend/data/nh_house_districts.json
BASE_TO_FLOTS, TOWN_TO_FLOTS = load_floterials()
REPS_BY_DIST, REP_INFO, ISSUES = load_votes()

# Build polygon list for point-in-polygon (base districts)
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

# ----------------------------
# Geocoding + helpers
# ----------------------------
def _sanitize_address(a: str) -> str:
    if not a:
        return a
    # strip unit/apt/suite/# parts that confuse geocoders
    return re.sub(r",?\s*(apt|apartment|unit|ste|suite|#)\s*[^\s,]+", "", a, flags=re.I)

def _split_addr(one_line: str):
    """
    Extract street, city (NH only), and ZIP if present; normalize 'West Lebanon' → 'Lebanon'.
    Works for '95 Alta Blvd, Lebanon, NH 03766' and bare '95 Alta Blvd'.
    """
    s = (one_line or "").strip()
    street = s.split(",", 1)[0].strip()
    m_city = re.search(r",\s*([^,]+?),\s*NH\b", s, flags=re.I)
    city = (m_city.group(1).strip() if m_city else "") or ""
    city_up = city.upper()
    if city_up in {"WEST LEBANON", "W LEBANON"}:
        city = "Lebanon"
    m_zip = re.search(r"\b(\d{5})(?:-\d{4})?\b", s)
    zip5 = m_zip.group(1) if m_zip else None
    return street, city, zip5

def _geocode_census_structured(street: str, city: Optional[str], zip5: Optional[str]):
    """Census structured endpoint (more reliable than oneline for new/private roads)."""
    if not street:
        return None, None, ""
    url = "https://geocoding.geo.census.gov/geocoder/locations/address"
    params = {"benchmark": "Public_AR_Current", "format": "json", "state": "NH", "street": street}
    if city: params["city"] = city
    if zip5: params["zip"] = zip5
    try:
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
        matches = (data.get("result") or {}).get("addressMatches") or []
        if not matches:
            return None, None, ""
        m0 = matches[0]
        coords = m0.get("coordinates") or {}
        lon = coords.get("x"); lat = coords.get("y")
        comps = m0.get("addressComponents") or {}
        town = (comps.get("city") or comps.get("place") or comps.get("county") or "").upper().strip()
        if lat is None or lon is None:
            return None, None, ""
        return float(lat), float(lon), town
    except Exception:
        return None, None, ""

def _geocode_osm_nominatim(q: str, city: Optional[str], zip5: Optional[str]):
    """
    OpenStreetMap Nominatim fallback. Set NHRF_NOMINATIM_EMAIL for polite usage.
    Only hit if Census fails.
    """
    if not q:
        return None, None, ""
    base = "https://nominatim.openstreetmap.org/search"
    # Build a concise query; OSM likes 'street, city, NH zip'
    parts = [q]
    if city: parts.append(city)
    parts.append("NH")
    if zip5: parts.append(zip5)
    params = {"q": ", ".join(parts), "format": "jsonv2", "limit": 1}
    headers = {
        "User-Agent": "NH-RepFinder/1.0 (+https://example.org)"
    }
    email = os.getenv("NHRF_NOMINATIM_EMAIL", "").strip()
    if email:
        params["email"] = email
    try:
        r = requests.get(base, params=params, headers=headers, timeout=8)
        r.raise_for_status()
        arr = r.json() or []
        if not arr:
            return None, None, ""
        j = arr[0]
        lat = float(j.get("lat")); lon = float(j.get("lon"))
        # Best-effort town from display_name; uppercase to match TOWN_TO_FLOTS keys
        disp = (j.get("display_name") or "")
        town = ""
        m = re.search(r",\s*([^,]+?),\s*New Hampshire\b", disp)
        if m: town = m.group(1).upper().strip()
        return lat, lon, town
    except Exception:
        return None, None, ""

def _geocode_census(one_line: str):
    """
    Try structured Census first (NH-scoped), then oneline, then OSM as last resort.
    This fixes failures for '95 Alta Blvd, Lebanon, NH 03766' and bare '95 Alta Blvd'.
    """
    if not one_line:
        return None, None, ""
    street, city, zip5 = _split_addr(one_line)

    # 1) Census structured
    lat, lon, town = _geocode_census_structured(street, city or None, zip5)
    if lat is not None and lon is not None:
        return lat, lon, town

    # 2) Census oneline (original)
    url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
    params = {"address": one_line, "benchmark": "Public_AR_Current", "format": "json"}
    try:
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
        matches = (data.get("result") or {}).get("addressMatches") or []
        if matches:
            m0 = matches[0]
            coords = m0.get("coordinates") or {}
            lon = coords.get("x"); lat = coords.get("y")
            comps = m0.get("addressComponents") or {}
            town = (comps.get("city") or comps.get("place") or comps.get("county") or "").upper().strip()
            if lat is not None and lon is not None:
                return float(lat), float(lon), town
    except Exception:
        pass

    # 3) OSM fallback
    return _geocode_osm_nominatim(street or one_line, city or None, zip5)

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

# ----------------------------
# Routes
# ----------------------------
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

# List issues for the pre-select UI (same shape as returned by /lookup)
@app.route("/issues")
def issues():
    return jsonify({"issues": ISSUES})

@app.route("/lookup")
def lookup():
    raw_addr = (request.args.get("address") or "").strip()
    lat_s = request.args.get("lat"); lon_s = request.args.get("lon")
    want_debug = (request.args.get("debug") == "1")

    # Parse direct lat/lon if provided
    latf = lonf = None
    if lat_s and lon_s:
        try:
            latf, lonf = float(lat_s), float(lon_s)
        except Exception:
            latf = lonf = None

    # Geocode if needed (with NH fallback)
    town_upper = ""
    if latf is None or lonf is None:
        addr = _sanitize_address(raw_addr)
        latf, lonf, town_upper = _geocode_census(addr)
        if (latf is None or lonf is None) and " NH" not in addr.upper():
            latf, lonf, town_upper = _geocode_census(addr + ", NH")
        if latf is None or lonf is None:
            return jsonify({"error": "could not geocode"}), 422

    # Base district from polygons (may be a code like GR15)
    base_code = _base_from_point(latf, lonf)
    base_name = code_to_district_name(base_code)
    csv_code3 = three_letter_from_name_or_code(base_name, base_code)  # e.g., 'GRA 15'

    # Build all candidate keys to match your CSV indexing
    candidate_keys = variant_keys(base_name, base_code, csv_code3)
    if csv_code3:
        candidate_keys.append(csv_code3.replace(" ", ""))  # 'GRA17'

    # Collect reps from base
    rep_ids: List[str] = []
    for key in candidate_keys:
        rep_ids.extend(REPS_BY_DIST.get(key, []))

    # Collect floterials mapped by base (name/code/3-letter) and by town
    flots = set()
    for s in [base_name, base_code, csv_code3]:
        if s:
            flots.update(BASE_TO_FLOTS.get(s, []))
            for key in variant_keys(s):
                flots.update(BASE_TO_FLOTS.get(key, []))
    if town_upper:
        flots.update(TOWN_TO_FLOTS.get(town_upper, []))

    # Add reps from each floterial: try label, its variants, and its 3-letter CSV code variants
    for f in sorted(list(flots)):
        keys = set(variant_keys(f))
        f3 = three_letter_from_name_or_code(f, "")
        if f3:
            for k in variant_keys(f3):
                keys.add(k)
            keys.add(f3.replace(" ", ""))  # 'GRA17'
        for key in keys:
            rep_ids.extend(REPS_BY_DIST.get(key, []))

    # De-dup, keep order
    seen, ordered = set(), []
    for r in rep_ids:
        if r not in seen:
            ordered.append(r); seen.add(r)
    reps = [REP_INFO[r] for r in ordered]

    # OPTIONAL: limit payload with ?issues=slug,slug
    only_param = (request.args.get("issues") or "").strip()
    only_slugs = {s.strip() for s in only_param.split(",") if s.strip()}
    issues_out = ISSUES
    if only_slugs:
        issues_out = [i for i in ISSUES if i.get("slug") in only_slugs]
        filtered_reps = []
        for rep in reps:
            votes = rep.get("votes") or {}
            filtered_votes = {k: v for k, v in votes.items() if k in only_slugs}
            filtered_reps.append({**rep, "votes": filtered_votes})
        reps = filtered_reps

    # Clean NaN-like floterials in the response
    flots = {f for f in flots if f and str(f).strip() and str(f).strip().lower() not in {"nan", "<nan>"}}

    resp = {
        "query": {"address": raw_addr, "lat": latf, "lon": lonf, "town": town_upper},
        "base_code": base_code,
        "base_district": base_name,
        "floterials": sorted(list(flots)),
        "issues": issues_out,
        "vote_columns": [i["slug"] for i in issues_out],  # back-compat
        "reps": reps
    }
    if want_debug:
        resp["debug"] = {
            "base_keys_tried": candidate_keys,
            "flots_found": sorted(list(flots)),
            "reps_count": len(reps)
        }
    return jsonify(resp)

# Serve the simple web UI from /web
@app.route("/")
def root():
    return send_from_directory(os.path.join(os.path.dirname(__file__), "..", "web"), "index.html")

@app.route("/<path:path>")
def static_proxy(path):
    web_dir = os.path.join(os.path.dirname(__file__), "..", "web")
    return send_from_directory(web_dir, path)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
