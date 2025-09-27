import os, requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from shapely.geometry import Point
from loader import load_geoindex, load_floterials, load_votes, district_key_variants, DATA_DIR

app = Flask(__name__, static_folder="../web", static_url_path="/")
CORS(app)

GEO = load_geoindex()
BASE_TO_FLOTS, TOWN_TO_FLOTS = load_floterials()
REPS_BY_DIST, REP_INFO, VOTE_COLS = load_votes()

def geocode(addr: str):
    """Try Census first (more tolerant from server), then Nominatim (with UA)."""
    try:
        url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
        params = {"address": f"{addr}, NH", "benchmark": "Public_AR_Current", "format": "json"}
        r = requests.get(url, params=params, timeout=6)
        r.raise_for_status()
        matches = r.json().get("result", {}).get("addressMatches", [])
        if matches:
            y = float(matches[0]["coordinates"]["y"])  # lat
            x = float(matches[0]["coordinates"]["x"])  # lon
            return y, x
    except Exception:
        pass
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": addr, "format": "json", "limit": 1, "addressdetails": 1},
            headers={"User-Agent": "NH-RepApp/1.0 (contact: admin@example.com)"},
            timeout=6
        )
        r.raise_for_status()
        j = r.json()
        if j:
            return float(j[0]["lat"]), float(j[0]["lon"])
    except Exception:
        pass
    return None

@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "counts": {
            "polygons": len(GEO.items),
            "base_to_flots": len(BASE_TO_FLOTS),
            "town_to_flots": len(TOWN_TO_FLOTS),
            "reps": len(REP_INFO),
            "vote_columns": len(VOTE_COLS),
        }
    })

@app.get("/lookup")
def lookup():
    addr = request.args.get("address","").strip()
    lat  = request.args.get("lat", type=float)
    lon  = request.args.get("lon", type=float)

    if addr and (lat is None or lon is None):
        loc = geocode(addr)
        if not loc:
            return jsonify({"error":"geocode_failed"}), 422
        lat, lon = loc

    if lat is None or lon is None:
        return jsonify({"error":"no_location"}), 422

    pt = Point(lon, lat)
    base_codes = GEO.lookup(pt)

    # expand floterials by base
    flots = set()
    for b in base_codes:
        for k in district_key_variants(b):
            flots.update(BASE_TO_FLOTS.get(k, []))

    # collect reps
    rep_ids = []
    for d in list(base_codes) + list(flots):
        for k in district_key_variants(d):
            rep_ids.extend(REPS_BY_DIST.get(k, []))
    reps = [REP_INFO[r] for r in dict.fromkeys(rep_ids)]

    return jsonify({
        "query": {"address": addr, "lat": lat, "lon": lon},
        "base_district": base_codes[0] if base_codes else None,
        "floterials": sorted(list(flots)),
        "vote_columns": VOTE_COLS,
        "reps": reps
    })

@app.get("/")
def home():
    return send_from_directory(app.static_folder, "index.html")
