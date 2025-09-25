# ELI5: load polygons, build spatial index, load CSV lookups and votes.

import json, os
import pandas as pd
from shapely.geometry import shape, Point
from shapely.prepared import prep

# Optional fast index
try:
    from rtree import index as rtree_index
    HAS_RTREE = True
except Exception:
    HAS_RTREE = False

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
GJ_PATH = os.path.join(DATA_DIR, "nh_house_districts.json")
FLOT_BY_BASE = os.path.join(DATA_DIR, "floterial_by_base.csv")
FLOT_BY_TOWN = os.path.join(DATA_DIR, "floterial_by_town.csv")
VOTES_CSV = os.path.join(DATA_DIR, "house_key_votes.csv")

class DistrictIndex:
    def __init__(self, features):
        # Store: [(geom, props, prepared_geom)]
        self.features = []
        for f in features:
            geom = shape(f["geometry"])
            props = f.get("properties", {})
            base_id = props.get("basehse22") or props.get("district") or props.get("DISTRICT") or props.get("name")
            props["_district_id"] = str(base_id)
            self.features.append((geom, props, prep(geom)))

        # Optional R-tree over polygon bounds
        self._rt = None
        if HAS_RTREE:
            idx = rtree_index.Index()
            for i, (geom, _, __) in enumerate(self.features):
                idx.insert(i, geom.bounds)
            self._rt = idx

    def pip_first(self, lon, lat):
        p = Point(lon, lat)
        # R-tree candidate list or full scan
        candidates = range(len(self.features))
        if self._rt:
            candidates = self._rt.intersection((lon, lat, lon, lat))
        for i in candidates:
            geom, props, prepared = self.features[i]
            # Use prepared.contains OR covers; points-on-border are tricky
            if prepared.covers(p):
                return props["_district_id"], props
        return None, None

def load_geoindex():
    with open(GJ_PATH, "r", encoding="utf-8") as f:
        gj = json.load(f)
    feats = gj["features"]
    return DistrictIndex(feats)

def load_floterials():
    """
    Returns two maps:
      base_to_flots: dict[str, set[str]]
      town_to_flots: dict[str, set[str]]   (town name normalized uppercase)
    """
    base_to_flots = {}
    town_to_flots = {}
    if os.path.exists(FLOT_BY_BASE):
        dfb = pd.read_csv(FLOT_BY_BASE, dtype=str).fillna("")
        # Expect columns like base_district, floterial_district (or similar)
        bcol = next((c for c in dfb.columns if c.lower().startswith("base")), "base_district")
        fcol = next((c for c in dfb.columns if c.lower().startswith("flot")), "floterial_district")
        for _, r in dfb.iterrows():
            b = str(r[bcol]).strip()
            f = str(r[fcol]).strip()
            if not b or not f: 
                continue
            base_to_flots.setdefault(b, set()).add(f)

    if os.path.exists(FLOT_BY_TOWN):
        dft = pd.read_csv(FLOT_BY_TOWN, dtype=str).fillna("")
        # Expect columns like town, floterial_district
        tcol = next((c for c in dft.columns if c.lower() in ("town", "municipality")), "town")
        fcol = next((c for c in dft.columns if c.lower().startswith("flot")), "floterial_district")
        for _, r in dft.iterrows():
            t = str(r[tcol]).strip().upper()
            f = str(r[fcol]).strip()
            if not t or not f:
                continue
            town_to_flots.setdefault(t, set()).add(f)

    return base_to_flots, town_to_flots

def load_votes():
    """
    Load wide votes CSV. Returns:
      reps_by_district: dict[str, list[rep_id]]
      rep_info: dict[rep_id] -> {name, party, district, votes{col->value}}
      vote_columns: list[str] (the key-vote headers)
    """
    df = pd.read_csv(VOTES_CSV, dtype=str).fillna("")
    # Normalize expected minimal columns:
    # openstates_person_id, name, district, Party, then vote columns
    lower = {c.lower(): c for c in df.columns}
    name_col = lower.get("name", "name")
    dist_col = lower.get("district", "district")
    party_col = next((c for c in df.columns if c.lower() in ("party","parties","affiliation")), "Party")
    # create a stable rep_id (prefer openstates id else name|district)
    if "openstates_person_id" in df.columns:
        rep_id_series = df["openstates_person_id"].replace("", pd.NA)
    else:
        rep_id_series = pd.Series([None]*len(df))

    vote_cols = [c for c in df.columns if c not in (name_col, dist_col, party_col, "openstates_person_id")]
    reps_by_district = {}
    rep_info = {}

    for _, row in df.iterrows():
        rid = row.get("openstates_person_id", "") or f"{row[name_col]}|{row[dist_col]}"
        nm = row[name_col].strip()
        dist = str(row[dist_col]).strip()
        party = row[party_col].strip()
        votes = {col: str(row[col]).strip() for col in vote_cols}
        rep_info[rid] = {"id": rid, "name": nm, "party": party, "district": dist, "votes": votes}
        reps_by_district.setdefault(dist, []).append(rid)

    return reps_by_district, rep_info, vote_cols
