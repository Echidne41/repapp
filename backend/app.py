import os, re, requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from shapely.geometry import Point
from loader import (
    load_geoindex, load_floterials, load_votes,
    district_key_variants, DATA_DIR
)

app = Flask(__name__, static_folder="../web", static_url_path="/")
CORS(app)

GEO = load_geoindex()
BASE_TO_FLOTS, TOWN_TO_FLOTS = load_floterials()
REPS_BY_DIST, REP_INFO, VOTE_COLS = load_votes()

# ---------- geocoding (forward + reverse) ----------
def geocode(addr: str):
    """Try Census first (server-friendly), then Nominatim. Return (lat, lon, town|None)."""
    # US Census forward
    try:
        url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
        params = {"address": f"{addr}, NH", "benchmark": "Public_AR_Current", "format": "json"}
        r = requests.get(url, params=params, timeout=6)
        r.raise_for_status()
        res = r.json().get("result", {})
        matches = res.get("addressMatches", [])
        if matches:
            m = matches[0]
            lat = float(m["coordinates"]["y"])
            lon = float(m["coordinates"]["x"])
            comps = m.get("addressComponents", {})
            town = (comps.get("city") or comps.get("municipality") or comps.get("placeName") or "")
            town = town.strip() or None
            return lat, lon, town
    except Exception:
        pass

    # Nominatim forward
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
            lat = float(j[0]["lat"])
            lon = float(j[0]["lon"])
            a = j[0].get("address", {})
            town = (a.get("town") or a.get("city") or a.get("village") or a.get("hamlet") or "")
            town = town.strip() or None
            return lat, lon, town
    except Exception:
        pass

    return None

def reverse_town(lat: float, lon: float):
    """Try to get town name from coordinates. Returns uppercase town or None."""
    # Census reverse → County Subdivision (town)
    try:
        url = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
        params = {"x": lon, "y": lat, "benchmark": "Public_AR_Current", "vintage": "Current_Current", "format": "json"}
        r = requests.get(url, params=params, timeout=6)
        r.raise_for_status()
        geog = r.json().get("result", {}).get("geographies", {})
        subs = geog.get("County Subdivisions", []) or geog.get("County Subdivision", [])
        if subs:
            name = subs[0].get("NAME")
            if name:
                return name.upper().strip()
    except Exception:
        pass

    # Nominatim reverse
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 12, "addressdetails": 1},
            headers={"User-Agent": "NH-RepApp/1.0 (contact: admin@example.com)"},
            timeout=6
        )
        r.raise_for_status()
        a = r.json().get("address", {})
        town = (a.get("town") or a.get("city") or a.get("village") or a.get("hamlet") or "")
        town = town.strip()
        return town.upper() if town else None
    except Exception:
        pass

    return None

# ---------- routes ----------
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

    town_upper = None

    if addr and (lat is None or lon is None):
        loc = geocode(addr)
        if not loc:
            return jsonify({"error":"geocode_failed"}), 422
        lat, lon, town = loc
        if town:
            town_upper = town.upper()

    if lat is None or lon is None:
        return jsonify({"error":"no_location"}), 422

    if not town_upper:
        town_upper = reverse_town(lat, lon)

    pt = Point(lon, lat)
    base_codes = GEO.lookup(pt)

    # floterials by base
    flots = set()
    for b in base_codes:
        for k in district_key_variants(b):
            flots.update(BASE_TO_FLOTS.get(k, []))

    # floterials by town
    if town_upper:
        flots.update(TOWN_TO_FLOTS.get(town_upper, []))

    # collect reps from base + flots
    rep_ids: List[str] = []
    for d in list(base_codes) + list(flots):
        for k in district_key_variants(d):
            rep_ids.extend(REPS_BY_DIST.get(k, []))
    reps = [REP_INFO[r] for r in dict.fromkeys(rep_ids)]  # uniq, preserve order

    return jsonify({
        "query": {"address": addr, "lat": lat, "lon": lon, "town": town_upper},
        "base_district": base_codes[0] if base_codes else None,
        "floterials": sorted(list(flots)),
        "vote_columns": VOTE_COLS,
        "reps": reps
    })

@app.get("/")
def home():
    return send_from_directory(app.static_folder, "index.html")
