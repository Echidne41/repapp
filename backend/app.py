from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
import os

from loader import load_geoindex, load_floterials, load_votes
from geo import geocode_address

load_dotenv()

app = Flask(__name__)
CORS(app)

# ---- One-time loads (fast after first) ----
GEO = load_geoindex()
BASE_TO_FLOTS, TOWN_TO_FLOTS = load_floterials()
REPS_BY_DIST, REP_INFO, VOTE_COLS = load_votes()

def _town_from_props(props):
    # Try common property names in your GeoJSON. Adjust if needed.
    for k in ("TOWN", "town", "MUNICIPAL", "municipality", "name", "TOWN_NAME"):
        if k in props and str(props[k]).strip():
            return str(props[k]).strip()
    return ""

@app.get("/health")
def health():
    return {"ok": True, "counts": {
        "polygons": len(GEO.features),
        "base_to_flots": len(BASE_TO_FLOTS),
        "town_to_flots": len(TOWN_TO_FLOTS),
        "reps": len(REP_INFO),
        "vote_columns": len(VOTE_COLS),
    }}

@app.get("/lookup")
def lookup():
    """
    /lookup?address=...  -> returns reps and votes for base + floterials
    Optional: /lookup?lat=..&lon=.. to bypass geocoder.
    """
    addr = request.args.get("address", "").strip()
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)

    if lat is None or lon is None:
        if not addr:
            return jsonify({"error": "Provide address or lat/lon"}), 400
        lon, lat = geocode_address(addr)
        if lon is None:
            return jsonify({"error": "Geocoding failed"}), 422

    # Base district from point-in-polygon
    base_id, base_props = GEO.pip_first(lon, lat)
    if not base_id:
        return jsonify({"error": "Point not inside any district"}), 404

    # Start set with base district
    districts = set([base_id])

    # Add floterials by base
    if base_id in BASE_TO_FLOTS:
        districts |= BASE_TO_FLOTS[base_id]

    # Add floterials by town (if we know town)
    town = _town_from_props(base_props).upper()
    if town and town in TOWN_TO_FLOTS:
        districts |= TOWN_TO_FLOTS[town]

    # Collect reps from all those districts (dedup)
    rep_ids = []
    for d in districts:
        rep_ids.extend(REPS_BY_DIST.get(str(d), []))

    # Deduplicate while preserving order
    seen = set()
    out_reps = []
    for rid in rep_ids:
        if rid not in seen and rid in REP_INFO:
            seen.add(rid)
            out_reps.append(REP_INFO[rid])

    res = {
        "query": {"address": addr, "lat": lat, "lon": lon},
        "base_district": base_id,
        "floterials": sorted(list(districts - {base_id})),
        "vote_columns": VOTE_COLS,
        "reps": out_reps,
    }
    return jsonify(res)

# (Optional) serve the simple web UI from ../web
@app.get("/")
def web_index():
    web_dir = os.path.join(os.path.dirname(__file__), "..", "web")
    return send_from_directory(web_dir, "index.html")
