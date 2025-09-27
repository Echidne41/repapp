import os
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from shapely.geometry import Point
from loader import load_geoindex, load_floterials, load_votes, district_key_variants, DATA_DIR
import requests

app = Flask(__name__, static_folder="../web", static_url_path="/")
CORS(app)

GEO = load_geoindex()
BASE_TO_FLOTS, TOWN_TO_FLOTS = load_floterials()
REPS_BY_DIST, REP_INFO, VOTE_COLS = load_votes()

NOMINATIM = "https://nominatim.openstreetmap.org/search"

def geocode(addr: str):
    r = requests.get(NOMINATIM, params={"q": addr, "format": "json", "limit": 1, "addressdetails": 1}, timeout=10)
    r.raise_for_status()
    j = r.json()
    if not j: return None
    return float(j[0]["lat"]), float(j[0]["lon"])

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
        try:
            lat, lon = geocode(addr)
        except Exception:
            return jsonify({"error":"geocode_failed"}), 422

    if lat is None or lon is None:
        return jsonify({"error":"no_location"}), 422

    pt = Point(lon, lat)
    # base district codes straight from polygons
    base_codes = GEO.lookup(pt)

    # expand with floterials-by-base and by-town (if any)
    flots = set()
    for b in base_codes:
        for k in district_key_variants(b):
            flots.update(BASE_TO_FLOTS.get(k, []))

    # collect reps using all variants of every district code we have
    rep_ids = []
    for d in list(base_codes) + list(flots):
        for k in district_key_variants(d):
            rep_ids.extend(REPS_BY_DIST.get(k, []))

    reps = [REP_INFO[r] for r in dict.fromkeys(rep_ids)]  # uniq, preserve order

    return jsonify({
        "query": {"address": addr, "lat": lat, "lon": lon},
        "base_district": base_codes[0] if base_codes else None,
        "floterials": sorted(list(flots)),
        "vote_columns": VOTE_COLS,
        "reps": reps
    })

@app.get("/")
def home():
    # simple mobile form
    return send_from_directory(app.static_folder, "index.html")

# convenience for raw data file download if you need to sanity-check
@app.get("/debug/file")
def debug_file():
    name = request.args.get("name")
    if not name: return {"error":"missing name"}, 400
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p): return {"error":"not found"}, 404
    with open(p, "r", encoding="utf-8") as f:
        return app.response_class(f.read(), mimetype="text/plain")
