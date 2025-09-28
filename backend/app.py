import os
from flask import Flask, jsonify, request, send_from_directory
from loader import (
    load_geoindex, load_floterials, load_votes,
)

app = Flask(__name__, static_folder=None)

# ---- Load data at startup ----
GEO = load_geoindex()                      # base districts (optional if your loader handles None)
BASE_TO_FLOTS, TOWN_TO_FLOTS = load_floterials()
REPS_BY_DIST, REP_INFO, ISSUES = load_votes()  # <-- NOTE: loader now returns ISSUES (not VOTE_COLS)

@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "counts": {
            "polygons": len(getattr(GEO, "items", [])),
            "base_to_flots": len(BASE_TO_FLOTS),
            "town_to_flots": len(TOWN_TO_FLOTS),
            "reps": len(REP_INFO),
            "issues": len(ISSUES),              # <-- changed from vote_columns
        }
    })

def _find_districts_for_point(lat, lon):
    """
    Minimal example: if your existing code already does this, keep it.
    Otherwise, just return empty and let your current base/floterial logic run.
    """
    # Stub — your existing lookup (base + floterials) should be here.
    return None, set()

@app.route("/lookup")
def lookup():
    addr = request.args.get("address", "").strip()
    lat = request.args.get("lat")
    lon = request.args.get("lon")
    town_upper = request.args.get("town", "")

    # Your existing resolver should compute:
    # base_codes (list) and flots (set). If you already have that code, leave it.
    base_codes, flots = [], set()

    # Aggregate reps from base + floterials, then map to struct the UI expects
    rep_ids = []
    for key in base_codes or []:
        rep_ids.extend(REPS_BY_DIST.get(key, []))
    for f in sorted(list(flots)):
        rep_ids.extend(REPS_BY_DIST.get(f, []))
    # de-dup but keep order
    seen = set(); ordered = []
    for r in rep_ids:
        if r not in seen:
            ordered.append(r); seen.add(r)

    reps = [REP_INFO[r] for r in ordered] if ordered else []

    return jsonify({
        "query": {"address": addr, "lat": lat, "lon": lon, "town": town_upper},
        "base_district": base_codes[0] if base_codes else None,
        "floterials": sorted(list(flots)),
        "issues": ISSUES,                                # <-- new: full issue objects for labels/links
        "vote_columns": [i["slug"] for i in ISSUES],     # <-- compatibility for any old UI code
        "reps": reps
    })


# ---- Static site (optional) ----
# If you're serving the simple web UI from /web, enable this:
@app.route("/")
def root():
    return send_from_directory(os.path.join(os.path.dirname(__file__), "..", "web"), "index.html")

@app.route("/<path:path>")
def static_proxy(path):
    web_dir = os.path.join(os.path.dirname(__file__), "..", "web")
    return send_from_directory(web_dir, path)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
