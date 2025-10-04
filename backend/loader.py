# backend/loader.py — stable; NV -> raw == ""
import os, io, re, json
from dataclasses import dataclass
from typing import Dict, List, Tuple
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("NHRF_DATA_DIR") or os.path.join(HERE, "data")

# Data files (env overrides optional)
VOTES_CSV    = os.path.join(DATA_DIR, "house_key_votes.csv")
ISSUES_CSV   = os.path.join(DATA_DIR, "issues.csv")
GEOJSON_BASE = os.path.join(DATA_DIR, "nh_house_districts.json")
FLOT_BASE    = os.path.join(DATA_DIR, "floterial_by_base.csv")
FLOT_TOWN    = os.path.join(DATA_DIR, "floterial_by_town.csv")

def district_key_variants(d: str) -> List[str]:
    """Tolerant keys for indexing by district string."""
    if not d:
        return []
    s = str(d).strip()
    return list(dict.fromkeys([
        s,
        s.upper(),
        re.sub(r"\s+", " ", s),
        re.sub(r"\s+", "", s.upper()),
    ]))

def _df_from_source(path_or_url: str) -> pd.DataFrame:
    if str(path_or_url).lower().startswith("http"):
        import requests
        r = requests.get(path_or_url, timeout=15)
        r.raise_for_status()
        return pd.read_csv(io.StringIO(r.text))
    return pd.read_csv(path_or_url)

# ---------- Normalizers ----------
def _norm(x):
    """None, real NaN, or literal 'nan'/case variants -> ''; else trimmed str."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return ""
    s = str(x).strip()
    return "" if s.lower() == "nan" else s

YES_TOKENS = {"Y","YES","YEA","AYE","PRO","FOR","APPROVE","APPROVED"}
NO_TOKENS  = {"N","NO","NAY","AGAINST","ANTI","REJECT","REJECTED"}
NOVOTE_TOKENS = {
    "NV","N/V","ABSTAIN","ABSTENTION","ABSENT","EXCUSED","PRESENT","—","","NA","N/A",
    "DID NOT VOTE","DIDN'T VOTE"
}

def _cell_to_yn(cell) -> str:
    if cell is None or (isinstance(cell, float) and pd.isna(cell)):
        return "NV"
    U = str(cell).strip().upper()
    if U.startswith("PRO-") or U.startswith("PRO "): return "Y"
    if U.startswith("ANTI-") or U.startswith("AGAINST"): return "N"
    if U in NOVOTE_TOKENS: return "NV"
    if U in YES_TOKENS: return "Y"
    if U in NO_TOKENS:  return "N"
    return "NV"

# ---------- Issues ----------
@dataclass
class IssueDef:
    slug: str
    csv_header: str
    label: str
    bill: str
    bill_url: str
    support_when: str  # 'Y' | 'N'

def _load_issues() -> List[IssueDef]:
    if not os.path.exists(ISSUES_CSV):
        return []
    df = pd.read_csv(ISSUES_CSV)
    out: List[IssueDef] = []
    for _, r in df.iterrows():
        slug       = _norm(r.get("slug"))
        csv_header = _norm(r.get("csv_header"))
        label      = _norm(r.get("label") or csv_header)
        bill       = _norm(r.get("bill"))
        bill_url   = _norm(r.get("bill_url"))
        sw         = _norm(r.get("support_when")).upper()
        out.append(IssueDef(
            slug=slug, csv_header=csv_header, label=label,
            bill=bill, bill_url=bill_url, support_when=("N" if sw == "N" else "Y")
        ))
    return out

# ---------- Public loaders ----------
def load_votes() -> Tuple[Dict[str, List[str]], Dict[str, dict], List[dict]]:
    """Return (reps_by_district, rep_info, issues_list)."""
    votes_src = os.environ.get("NHRF_VOTES_SRC") or VOTES_CSV
    if not (str(votes_src).lower().startswith("http") or os.path.exists(votes_src)):
        raise FileNotFoundError(f"Missing votes CSV: {votes_src}")
    df = _df_from_source(votes_src)

    name_col  = next(c for c in df.columns if c.lower() in ("name","rep","representative"))
    dist_col  = next(c for c in df.columns if "district" in c.lower())
    party_col = next(c for c in df.columns if "party" in c.lower())

    issues = _load_issues()
    if not issues:
        # Derive issues from CSV headers if issues.csv missing
        raw_cols = [c for c in df.columns if c not in (name_col, dist_col, party_col, "openstates_person_id")]
        def slugify(x): return re.sub(r"[^a-z0-9]+","_", x.lower()).strip("_")
        issues = [IssueDef(slug=slugify(c), csv_header=c, label=c, bill="", bill_url="", support_when="Y") for c in raw_cols]

    reps_by_district: Dict[str, List[str]] = {}
    rep_info: Dict[str, dict] = {}

    for _, row in df.iterrows():
        rid  = str(row.get("openstates_person_id") or f"{row[name_col]}|{row[dist_col]}")
        nm   = _norm(row.get(name_col))
        dist = _norm(row.get(dist_col))
        par  = _norm(row.get(party_col))

        votes = {}
        for it in issues:
            raw = row.get(it.csv_header)
            yn = _cell_to_yn(raw)
            if yn == "NV":
                stance = "no_vote"
            else:
                yes_means_support = (it.support_when == "Y")
                stance = "support" if ((yn == "Y") == yes_means_support) else "oppose"
            votes[it.slug] = {
                "stance": stance,
                "raw": ("" if yn == "NV" else _norm(raw)),  # <-- only change: blank raw for NV (no 'nan')
                "vote": yn
            }

        rep_info[rid] = {"id": rid, "name": nm, "party": par, "district": dist, "votes": votes}
        for key in district_key_variants(dist):
            reps_by_district.setdefault(key, []).append(rid)

    issues_out = [dict(
        slug=i.slug, label=i.label, bill=i.bill, bill_url=i.bill_url,
        support_when=i.support_when, csv_header=i.csv_header
    ) for i in issues]

    return reps_by_district, rep_info, issues_out

# ---- Geo index (unchanged) ----
class _GeoIndex:
    """Lightweight holder so /health can report polygon count."""
    def __init__(self, path: str):
        self.items: List[dict] = []
        self.path = path
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                gj = json.load(f)
            self.items = gj.get("features") or []

def load_geoindex() -> _GeoIndex:
    return _GeoIndex(GEOJSON_BASE)

def load_floterials() -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """Return (BASE_TO_FLOTS, TOWN_TO_FLOTS). Accepts common header spellings; same behavior."""
    base_to_flots: Dict[str, List[str]] = {}
    town_to_flots: Dict[str, List[str]] = {}

    if os.path.exists(FLOT_BASE):
        df = pd.read_csv(FLOT_BASE)
        for _, r in df.iterrows():
            base = _norm(r.get("base") or r.get("Base") or r.get("BASE") or
                         r.get("base_district") or r.get("Base_District") or r.get("BASE_DISTRICT"))
            flot = _norm(r.get("floterial") or r.get("Floterial") or r.get("FLOTERIAL") or
                         r.get("floterial_district") or r.get("Floterial_District") or r.get("FLOTERIAL_DISTRICT"))
            if base and flot:
                base_to_flots.setdefault(base, []).append(flot)

    if os.path.exists(FLOT_TOWN):
        df = pd.read_csv(FLOT_TOWN)
        for _, r in df.iterrows():
            town = _norm(r.get("town") or r.get("Town") or r.get("TOWN")).upper()
            flot = _norm(r.get("floterial") or r.get("Floterial") or r.get("FLOTERIAL") or
                         r.get("floterial_district") or r.get("Floterial_District") or r.get("FLOTERIAL_DISTRICT"))
            if town and flot:
                town_to_flots.setdefault(town, []).append(flot)

    return base_to_flots, town_to_flots
