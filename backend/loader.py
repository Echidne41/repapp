import os, json
import pandas as pd
from shapely.geometry import Polygon, MultiPolygon, Point
from shapely.geometry import shape as _shape
from shapely.ops import unary_union
from shapely.validation import make_valid
from shapely.prepared import prep



# ---- Data dir resolution: ENV -> backend/data -> ../data
HERE = os.path.dirname(__file__)
DATA_DIR = os.getenv("NHRF_DATA_DIR") or os.path.join(HERE, "data")
if not os.path.exists(os.path.join(DATA_DIR, "nh_house_districts.json")):
    DATA_DIR = os.path.abspath(os.path.join(HERE, "..", "data"))

GJ_PATH      = os.path.join(DATA_DIR, "nh_house_districts.json")
FLOT_BY_BASE = os.path.join(DATA_DIR, "floterial_by_base.csv")
FLOT_BY_TOWN = os.path.join(DATA_DIR, "floterial_by_town.csv")
VOTES_CSV    = os.path.join(DATA_DIR, "house_key_votes.csv")

COUNTY_ABBR = {
    "BELKNAP":"BE","CARROLL":"CA","CHESHIRE":"CH","COOS":"CO",
    "GRAFTON":"GR","HILLSBOROUGH":"HI","MERRIMACK":"ME",
    "ROCKINGHAM":"RO","STRAFFORD":"ST","SULLIVAN":"SU"
}
ABBR_TO_COUNTY = {v:k.title() for k,v in COUNTY_ABBR.items()}

def district_key_variants(txt: str):
    """Return all reasonable keys for one district label/code."""
    if not txt:
        return set()
    s = str(txt).strip()
    out = {s, s.upper(), s.title()}
    # “Merrimack 18” -> “ME18”
    parts = s.strip().upper().split()
    if len(parts) == 2 and parts[0] in COUNTY_ABBR:
        code = COUNTY_ABBR[parts[0]] + ''.join(ch for ch in parts[1] if ch.isdigit())
        if code:
            out |= {code, code.upper()}
    # “ME18” -> “Merrimack 18”
    up = s.upper()
    if len(up) >= 3 and up[:2] in ABBR_TO_COUNTY and up[2:].isdigit():
        county = ABBR_TO_COUNTY[up[:2]]
        num = str(int(up[2:]))
        longform = f"{county} {num}"
        out |= {longform, longform.upper(), longform.title()}
    return {k.strip() for k in out if k.strip()}

def _f_ring(ring):
    out = []
    for pt in ring:
        x, y = pt[0], pt[1]
        out.append((float(x), float(y)))
    return out

def _poly_from_coords(coords):
    if not coords:
        return Polygon()
    ext  = _f_ring(coords[0])
    holes = [_f_ring(r) for r in coords[1:]] if len(coords) > 1 else []
    return Polygon(ext, holes=holes)

def _safe_shape(geom):
    """Return a valid (Multi)Polygon from messy GeoJSON."""
    try:
        g = _shape(geom)
    except Exception:
        t = (geom or {}).get("type")
        if t == "Polygon":
            g = _poly_from_coords(geom.get("coordinates", []))
        elif t == "MultiPolygon":
            polys = []
            for poly in geom.get("coordinates", []):
                if poly: polys.append(_poly_from_coords(poly))
            g = MultiPolygon(polys)
        elif t == "GeometryCollection":
            # Collect any polygonal parts
            parts = []
            for sub in (geom.get("geometries") or []):
                try:
                    sg = _safe_shape(sub)
                    if not sg.is_empty: parts.append(sg)
                except Exception:
                    pass
            g = unary_union(parts) if parts else Polygon()
        else:
            # last-ditch: try again
            g = _shape(geom)

    # Repair invalid/self-intersecting shapes
    try:
        if not g.is_valid:
            g = make_valid(g)
    except Exception:
        try:
            g = g.buffer(0)  # classic fix
        except Exception:
            pass
    return g

class DistrictIndex:
    def __init__(self, features):
        self.features = []  # (geom, props, prepared)
        skipped = 0
        for i, f in enumerate(features):
            try:
                geom = _safe_shape(f.get("geometry"))
                if geom.is_empty:
                    skipped += 1; continue
                props = f.get("properties", {}) or {}
                base_id = (props.get("basehse22") or props.get("DISTRICT")
                           or props.get("district") or props.get("name")
                           or f"IDX_{i}")
                props["_district_id"] = str(base_id)
                self.features.append((geom, props, prep(geom)))
            except Exception as e:
                print(f"[loader] WARN skip feature {i}: {e}")
                skipped += 1
        print(f"[loader] Loaded {len(self.features)} districts, skipped {skipped}")

    def pip_first(self, lon, lat):
        p = Point(float(lon), float(lat))
        for geom, props, prepared in self.features:
            if prepared.covers(p):
                return props["_district_id"], props
        return None, None

def load_geoindex():
    if not os.path.exists(GJ_PATH):
        raise FileNotFoundError(f"Missing GeoJSON: {GJ_PATH}")
    with open(GJ_PATH, "r", encoding="utf-8") as f:
        gj = json.load(f)
    feats = gj["features"] if isinstance(gj, dict) and "features" in gj else gj
    return DistrictIndex(feats)

def load_floterials():
    base_to_flots, town_to_flots = {}, {}
    if os.path.exists(FLOT_BY_BASE):
        dfb = pd.read_csv(FLOT_BY_BASE, dtype=str).fillna("")
        bcol = next((c for c in dfb.columns if c.lower().startswith("base")), "base_district")
        fcol = next((c for c in dfb.columns if c.lower().startswith("flot")), "floterial_district")
        for _, r in dfb.iterrows():
            b = str(r.get(bcol, "")).strip(); f = str(r.get(fcol, "")).strip()
            if b and f: base_to_flots.setdefault(b, set()).add(f)
    if os.path.exists(FLOT_BY_TOWN):
        dft = pd.read_csv(FLOT_BY_TOWN, dtype=str).fillna("")
        tcol = next((c for c in dft.columns if c.lower() in ("town","municipality")), "town")
        fcol = next((c for c in dft.columns if c.lower().startswith("flot")), "floterial_district")
        for _, r in dft.iterrows():
            t = str(r.get(tcol, "")).strip().upper(); f = str(r.get(fcol, "")).strip()
            if t and f: town_to_flots.setdefault(t, set()).add(f)
    print(f"[loader] floterials base={len(base_to_flots)} town={len(town_to_flots)}")
    return base_to_flots, town_to_flots

def load_votes():
    if not os.path.exists(VOTES_CSV):
        raise FileNotFoundError(f"Missing votes CSV: {VOTES_CSV}")
    df = pd.read_csv(VOTES_CSV, dtype=str).fillna("")
    lower = {c.lower(): c for c in df.columns}
    name_col  = lower.get("name","name")
    dist_col  = lower.get("district","district")
    party_col = next((c for c in df.columns if c.lower() in ("party","parties","affiliation")), "Party")
    vote_cols = [c for c in df.columns if c not in (name_col, dist_col, party_col, "openstates_person_id")]
    reps_by_district, rep_info = {}, {}
    for _, row in df.iterrows():
        rid = row.get("openstates_person_id","") or f"{row[name_col]}|{row[dist_col]}"
        nm  = str(row[name_col]).strip()
        dist= str(row[dist_col]).strip()
        par = str(row[party_col]).strip()
        votes = {col: str(row[col]).strip() for col in vote_cols}
        rep_info[rid] = {"id": rid, "name": nm, "party": par, "district": dist, "votes": votes}
        reps_by_district.setdefault(dist, []).append(rid)
    print(f"[loader] votes reps={len(rep_info)} cols={len(vote_cols)}")
    return reps_by_district, rep_info, vote_cols
