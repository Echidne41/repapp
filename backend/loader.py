# backend/loader.py
# Robust loader for districts + floterials + key votes.
# - Finds data in backend/data OR ../data
# - Safely builds Polygon/MultiPolygon (coerces coords to float)
# - Skips truly bad features instead of crashing
# - Minimal deps (no rtree needed)

import os
import json
import pandas as pd
from shapely.geometry import Polygon, MultiPolygon, Point
from shapely.geometry import shape as _shape
from shapely.prepared import prep

# ---------------------------
# Data paths (with fallback)
# ---------------------------
HERE = os.path.dirname(__file__)
DATA_DIR = os.path.join(HERE, "data")
if not os.path.exists(os.path.join(DATA_DIR, "nh_house_districts.json")):
    # Repo-root /data fallback
    DATA_DIR = os.path.abspath(os.path.join(HERE, "..", "data"))

GJ_PATH      = os.path.join(DATA_DIR, "nh_house_districts.json")
FLOT_BY_BASE = os.path.join(DATA_DIR, "floterial_by_base.csv")
FLOT_BY_TOWN = os.path.join(DATA_DIR, "floterial_by_town.csv")
VOTES_CSV    = os.path.join(DATA_DIR, "house_key_votes.csv")


# ---------------------------
# Geometry helpers
# ---------------------------
def _f_ring(ring):
    # force numeric tuples; tolerate strings/decimals
    out = []
    for pt in ring:
        # pt can be (x,y) or [x,y]
        x, y = pt[0], pt[1]
        out.append((float(x), float(y)))
    return out

def _safe_shape(geom):
    """
    Try shapely.shape; if it chokes on odd MultiPolygons, rebuild manually.
    """
    try:
        return _shape(geom)
    except Exception:
        t = (geom or {}).get("type")
        coords = geom.get("coordinates", []) if geom else []
        if t == "Polygon":
            if not coords:
                return Polygon()
            ext = _f_ring(coords[0])
            holes = [_f_ring(r) for r in coords[1:]] if len(coords) > 1 else []
            return Polygon(ext, holes=holes)
        if t == "MultiPolygon":
            polys = []
            for poly in coords:
                if not poly:
                    continue
                ext = _f_ring(poly[0])
                holes = [_f_ring(r) for r in poly[1:]] if len(poly) > 1 else []
                polys.append(Polygon(ext, holes=holes))
            return MultiPolygon(polys)
        # Last try: pass through
        return _shape(geom)


# ---------------------------
# District spatial index
# ---------------------------
class DistrictIndex:
    def __init__(self, features):
        self.features = []  # list of (geom, props, prepared)
        skipped = 0
        for i, f in enumerate(features):
            try:
                geom = _safe_shape(f.get("geometry"))
                if geom.is_empty:
                    skipped += 1
                    continue
                props = f.get("properties", {}) or {}
                base_id = (
                    props.get("basehse22")
                    or props.get("DISTRICT")
                    or props.get("district")
                    or props.get("name")
                    or f"IDX_{i}"
                )
                props["_district_id"] = str(base_id)
                self.features.append((geom, props, prep(geom)))
            except Exception as e:
                print(f"[loader] WARN skip feature {i}: {e}")
                skipped += 1
        print(f"[loader] Loaded {len(self.features)} districts, skipped {skipped}")

    def pip_first(self, lon, lat):
        p = Point(float(lon), float(lat))
        # simple scan w/ prepared geometries (fast enough for NH)
        for geom, props, prepared in self.features:
            if prepared.covers(p):
                return props["_district_id"], props
        return None, None


def load_geoindex():
    if not os.path.exists(GJ_PATH):
        raise FileNotFoundError(f"Missing GeoJSON: {GJ_PATH}")
    with open(GJ_PATH, "r", encoding="utf-8") as f:
        gj = json.load(f)

    # Feature collection or plain list
    if isinstance(gj, dict) and "features" in gj:
        feats = gj["features"]
    elif isinstance(gj, list):
        feats = gj
    else:
        raise ValueError("Unsupported GeoJSON structure for districts")

    return DistrictIndex(feats)


# ---------------------------
# Floterial lookups
# ---------------------------
def load_floterials():
    """
    Returns:
      base_to_flots: dict[str, set[str]]
      town_to_flots: dict[str, set[str]]  (town uppercased)
    """
    base_to_flots, town_to_flots = {}, {}

    if os.path.exists(FLOT_BY_BASE):
        dfb = pd.read_csv(FLOT_BY_BASE, dtype=str).fillna("")
        # detect column names
        base_col = next((c for c in dfb.columns if c.lower().startswith("base")), "base_district")
        flot_col = next((c for c in dfb.columns if c.lower().startswith("flot")), "floterial_district")
        for _, r in dfb.iterrows():
            b = str(r.get(base_col, "")).strip()
            f = str(r.get(flot_col, "")).strip()
            if b and f:
                base_to_flots.setdefault(b, set()).add(f)

    if os.path.exists(FLOT_BY_TOWN):
        dft = pd.read_csv(FLOT_BY_TOWN, dtype=str).fillna("")
        town_col = next((c for c in dft.columns if c.lower() in ("town", "municipality")), "town")
        flot_col = next((c for c in dft.columns if c.lower().startswith("flot")), "floterial_district")
        for _, r in dft.iterrows():
            t = str(r.get(town_col, "")).strip().upper()
            f = str(r.get(flot_col, "")).strip()
            if t and f:
                town_to_flots.setdefault(t, set()).add(f)

    print(f"[loader] floterials: base={len(base_to_flots)} town={len(town_to_flots)}")
    return base_to_flots, town_to_flots


# ---------------------------
# Votes / roster
# ---------------------------
def load_votes():
    """
    Load wide votes CSV.

    Returns:
      reps_by_district: dict[str, list[rep_id]]
      rep_info: dict[rep_id] -> {id,name,party,district,votes{col->val}}
      vote_columns: list[str]
    """
    if not os.path.exists(VOTES_CSV):
        raise FileNotFoundError(f"Missing votes CSV: {VOTES_CSV}")
    df = pd.read_csv(VOTES_CSV, dtype=str).fillna("")

    # detect standard columns
    lower = {c.lower(): c for c in df.columns}
    name_col  = lower.get("name", "name")
    dist_col  = lower.get("district", "district")
    party_col = next((c for c in df.columns if c.lower() in ("party","parties","affiliation")), "Party")

    vote_cols = [c for c in df.columns if c not in (name_col, dist_col, party_col, "openstates_person_id")]

    reps_by_district = {}
    rep_info = {}

    for _, row in df.iterrows():
        rid = row.get("openstates_person_id", "") or f"{row[name_col]}|{row[dist_col]}"
        nm = str(row[name_col]).strip()
        dist = str(row[dist_col]).strip()
        party = str(row[party_col]).strip()
        votes = {col: str(row[col]).strip() for col in vote_cols}
        rep_info[rid] = {"id": rid, "name": nm, "party": party, "district": dist, "votes": votes}
        reps_by_district.setdefault(dist, []).append(rid)

    print(f"[loader] votes: reps={len(rep_info)} vote_cols={len(vote_cols)}")
    return reps_by_district, rep_info, vote_cols
