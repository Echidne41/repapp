import json, os, re
from dataclasses import dataclass
from typing import Dict, List, Tuple, Iterable
import pandas as pd
from shapely.geometry import shape, Point

# --------- paths ---------
HERE = os.path.dirname(__file__)
DATA_DIR = os.environ.get("NHRF_DATA_DIR") or os.path.join(HERE, "data")
GJ_PATH   = os.path.join(DATA_DIR, "nh_house_districts.json")
VOTES_CSV = os.path.join(DATA_DIR, "house_key_votes.csv")
FLOT_BASE = os.path.join(DATA_DIR, "floterial_by_base.csv")
FLOT_TOWN = os.path.join(DATA_DIR, "floterial_by_town.csv")

# --------- district key normalization ---------
COUNTY_2_TO_3 = {
    "BE":"BEL", "CA":"CAR", "CH":"CHE", "CO":"COO",
    "GR":"GRA", "HI":"HIL", "ME":"MER",
    "RO":"ROC", "ST":"STR", "SU":"SUL",
}
COUNTY_3_TO_2 = {v:k for k,v in COUNTY_2_TO_3.items()}
COUNTY_LONG   = {
    "BEL":"BELKNAP", "CAR":"CARROLL", "CHE":"CHESHIRE", "COO":"COOS",
    "GRA":"GRAFTON", "HIL":"HILLSBOROUGH", "MER":"MERRIMACK",
    "ROC":"ROCKINGHAM", "STR":"STRAFFORD", "SUL":"SULLIVAN",
}
LONG_TO_3 = {v:k for k,v in COUNTY_LONG.items()}

def _digits(s:str)->str: return "".join(ch for ch in s if ch.isdigit())

def district_key_variants(txt: str) -> set:
    """Generate many equivalent keys for a district."""
    if not txt: return set()
    s = str(txt).strip()
    out = {s, s.upper(), s.title()}

    U = s.upper().replace(".", " ").replace("_", " ").strip()
    U = re.sub(r"\s+", " ", U)

    # Form: 'MERRIMACK 18' (or hyphen)
    m = re.match(r"^(BELKNAP|CARROLL|CHESHIRE|COOS|GRAFTON|HILLSBOROUGH|MERRIMACK|ROCKINGHAM|STRAFFORD|SULLIVAN)[ -]*(\d+)$", U)
    if m:
        long_cnt, num = m.group(1), str(int(m.group(2)))
        c3 = LONG_TO_3[long_cnt]
        c2 = COUNTY_3_TO_2[c3]
        out |= {
            f"{long_cnt} {num}", f"{long_cnt}-{num}",
            f"{c3} {num}", f"{c2}{num}", f"{c3}{num}",  # allow no-space codes too
        }

    # Form: 'MER 18' (or 'MER18')
    m = re.match(r"^([A-Z]{3})\s*(\d+)$", U)
    if m and m.group(1) in COUNTY_3_TO_2:
        c3, num = m.group(1), str(int(m.group(2)))
        long_cnt = COUNTY_LONG[c3]
        c2 = COUNTY_3_TO_2[c3]
        out |= {f"{c3} {num}", f"{c2}{num}", f"{long_cnt} {num}", f"{long_cnt}-{num}"}

    # Form: 'ME18'
    m = re.match(r"^([A-Z]{2})(\d+)$", U)
    if m and m.group(1) in COUNTY_2_TO_3:
        c2, num = m.group(1), str(int(m.group(2)))
        c3 = COUNTY_2_TO_3[c2]
        long_cnt = COUNTY_LONG[c3]
        out |= {f"{c2}{num}", f"{c3} {num}", f"{long_cnt} {num}", f"{long_cnt}-{num}"}

    # Strip parentheses
    U2 = re.sub(r"\s*\(.*?\)\s*$", "", U)
    if U2 != U:
        out |= {U2, U2.title()}

    return {k.strip() for k in out if k and k.strip()}

# --------- geometry index ---------
@dataclass
class DistrictPoly:
    code: str  # e.g. ME18, MER 18, etc.
    geom: object

class DistrictIndex:
    def __init__(self, feats: Iterable[dict]):
        self.items: List[DistrictPoly] = []
        for f in feats:
            try:
                geom = shape(f["geometry"])
                code = str(f["properties"].get("CODE") or f["properties"].get("code") or f["properties"].get("District") or "").strip()
                if not code:
                    # Try to build from fields like county + number
                    county = str(f["properties"].get("county") or "").strip().upper()
                    num = str(f["properties"].get("number") or "").strip()
                    if county and num and county in LONG_TO_3:
                        code = COUNTY_3_TO_2[LONG_TO_3[county]] + str(int(_digits(num) or "0"))
                if not code: 
                    continue
                self.items.append(DistrictPoly(code=code, geom=geom))
            except Exception as e:
                # Skip anything Shapely can't build
                continue

    def lookup(self, pt: Point) -> List[str]:
        hits = []
        for it in self.items:
            try:
                if it.geom.contains(pt):
                    hits.append(it.code)
            except Exception:
                pass
        return hits

# --------- loaders ---------
def load_geoindex() -> DistrictIndex:
    if not os.path.exists(GJ_PATH):
        raise FileNotFoundError(f"Missing GeoJSON: {GJ_PATH}")
    with open(GJ_PATH, "r", encoding="utf-8") as f:
        gj = json.load(f)
    feats = gj["features"] if "features" in gj else gj
    return DistrictIndex(feats)

def load_floterials() -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    def load_csv(path):
        return pd.read_csv(path) if os.path.exists(path) else pd.DataFrame()
    dfb = load_csv(FLOT_BASE)
    dft = load_csv(FLOT_TOWN)
    base_to_flots = {}
    town_to_flots = {}
    if not dfb.empty:
        for _, r in dfb.iterrows():
            base = str(r.get("base_district") or r.get("base") or "").strip()
            flot = str(r.get("floterial") or r.get("flot") or "").strip()
            for k in district_key_variants(base):
                base_to_flots.setdefault(k, []).append(flot)
    if not dft.empty:
        for _, r in dft.iterrows():
            town = str(r.get("town") or "").strip().upper()
            flot = str(r.get("floterial") or r.get("flot") or "").strip()
            if town:
                town_to_flots.setdefault(town, []).append(flot)
    return base_to_flots, town_to_flots

def load_votes() -> Tuple[Dict[str, List[str]], Dict[str, dict], List[str]]:
    if not os.path.exists(VOTES_CSV):
        raise FileNotFoundError(f"Missing votes CSV: {VOTES_CSV}")
    df = pd.read_csv(VOTES_CSV)
    # flexible column names
    name_col  = next(c for c in df.columns if c.lower() in ("name","rep","representative"))
    dist_col  = next(c for c in df.columns if "district" in c.lower())
    party_col = next(c for c in df.columns if "party" in c.lower())

    vote_cols = [c for c in df.columns if c not in (name_col, dist_col, party_col, "openstates_person_id")]
    reps_by_district: Dict[str, List[str]] = {}
    rep_info: Dict[str, dict] = {}

    for _, row in df.iterrows():
        rid = str(row.get("openstates_person_id") or f"{row[name_col]}|{row[dist_col]}")
        nm  = str(row[name_col]).strip()
        dist= str(row[dist_col]).strip()
        par = str(row[party_col]).strip()
        votes = {col: ("" if pd.isna(row[col]) else str(row[col]).strip()) for col in vote_cols}

        rep_info[rid] = {"id": rid, "name": nm, "party": par, "district": dist, "votes": votes}
        for key in district_key_variants(dist):
            reps_by_district.setdefault(key, []).append(rid)

    return reps_by_district, rep_info, vote_cols
