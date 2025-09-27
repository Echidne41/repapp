from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
import os

load_dotenv()

from loader import load_geoindex, load_floterials, load_votes
from geo import geocode_address

app = Flask(__name__)
CORS(app)

# Load once at boot
GEO = load_geoindex()
BASE_TO_FLOTS, TOWN_TO_FLOTS = load_floterials()
REPS_BY_DIST, REP_INFO, VOTE_COLS = load_votes()

def _town_from_props(props):
    for k in ("TOWN","town","MUNICIPAL","municipality","name","TOWN_NAME"):
        v = props.get(k)
        if v: return str(v).strip()
    return ""

@app.get("/health")
def health():
    return {
        "ok": True,
        "counts": {
            "polygons": len(GEO.features),
            "base_to_flots": len(BASE_TO_FLOTS),
            "town_to_flots": len(TOWN_TO_FLOTS),
            "reps": len(REP_INFO),
            "vote_columns": len(VOTE_COLS),
        }
    }

@app.get("/lookup")
def lookup():
    addr = request.args.get("address","").strip()
    lat  = request.args.get("lat", type=float)
    lon  = request.args.get("lon", type=float)

    if (lat is None or lon is None) and not addr:
        return jsonify({"error":"Provide address or lat/lon"}), 400

    if lat is None or lon is None:
        lon, lat = geocode_address(addr)
        if lon is None:
            return jsonify({"error":"Geocoding failed"}), 422

    base_id, base_props = GEO.pip_first(lon, lat)
    if not base_id:
        return jsonify({"error":"Point not inside any district"}), 404

    districts = set([base_id])
    if base_id in BASE_TO_FLOTS: districts |= BASE_TO_FLOTS[base_id]
    town = _town_from_props(base_props).upper()
    if town and town in TOWN_TO_FLOTS: districts |= TOWN_TO_FLOTS[town]

    # build all keys for each district we found (base + floterials)
    from loader import district_key_variants  # add this import at top with others

    rep_ids = []
    for d in districts:
        for k in district_key_variants(str(d)):
            rep_ids.extend(REPS_BY_DIST.get(k, []))


    seen, out = set(), []
    for rid in rep_ids:
        if rid in REP_INFO and rid not in seen:
            seen.add(rid); out.append(REP_INFO[rid])

    return jsonify({
        "query": {"address": addr, "lat": lat, "lon": lon},
        "base_district": base_id,
        "floterials": sorted(list(districts - {base_id})),
        "vote_columns": VOTE_COLS,
        "reps": out,
    })

# Serve the simple UI (repo-root /web or backend/web)
@app.get("/")
def web_index():
    here = os.path.dirname(__file__)
    p1 = os.path.abspath(os.path.join(here, "..", "web"))
    if os.path.exists(os.path.join(p1, "index.html")):
        return send_from_directory(p1, "index.html")
    p2 = os.path.join(here, "web")
    return send_from_directory(p2, "index.html")
