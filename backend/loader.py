# backend/loader.py
import os, io, re, json
from dataclasses import dataclass
from typing import Dict, List, Tuple
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("NHRF_DATA_DIR") or os.path.join(HERE, "data")

# Data file defaults (you can override with env vars later)
VOTES_CSV = os.path.join(DATA_DIR, "house_key_votes.csv")
ISSUES_CSV = os.path.join(DATA_DIR, "issues.csv")
GEOJSON_BASE = os.path.join(DATA_DIR, "nh_house_districts.json")  # base districts (optional)
FLOT_BASE = os.path.join(DATA_DIR, "floterial_by_base.csv")       # optional
FLOT_TOWN = os.path.join(DATA_DIR, "floterial_by_town.csv")       # optional

# ---------- helpers ----------
def district_key_variants(d: str) -> List[str]:
    """Tolerant keys for indexing by district string."""
    if not d:
        return []
    s = str(d).strip()
    return list(dict.fromkeys([  # preserve order, de-dup
        s,
        s.upper(),
        re.sub(r"\s+", " ", s),
        re.sub(r"\s+", "", s.upper()),
    ]))

def _df_from_source(path_or_url: str) -> pd.DataFrame:
    """Read CSV from local path or HTTP(S) URL."""
    if str(path_or_url).lower().startswith("http"):
        import requests
        r = requests.get(path_or_url, timeout=15)
        r.raise_for_status()
        return pd.read_csv(io.StringIO(r.text))
    return pd.read_csv(path_or_url)

# ---------- issues catalog ----------
@dataclass
class IssueDef:
    slug: str
    csv_header: str
    label: str
    bill: str
    bill_url: str
    support_when: str  # 'Y' (Yes means support) or 'N' (No means support)

def _load_issues() -> List[IssueDef]:
    if not os.path.exists(ISSUES_CSV):
        return []
    df = pd.read_csv(ISSUES_CSV)
    out: List[IssueDef] = []
    for _, r in df.iterrows():
        slug = str(r.get("slug") or "").strip()
        csv_header = str(r.get("csv_header") or "").strip()
        label = str(r.get("label") or csv_header).strip()
        bill = str(r.get("bill") or "").strip()
        bill_url = str(r.get("bill_url") or "").strip()
        sw = str(r.get("support_when") or "Y").strip().upper()
        out.append(IssueDef(
            slug=slug, csv_header=csv_header, label=label,
            bill=bill, bill_url=bill_url, support_when=("N" if sw == "N" else "Y")
        ))
    return out

# ---------- vote normalization ----------
YES_TOKENS = {"Y", "YES", "YEA", "AYE", "PRO", "FOR", "APPROVE", "APPROVED"}
NO_TOKENS = {"N", "NO", "NAY", "AGAINST", "ANTI", "REJECT", "REJECTED"}
NOVOTE_TOKENS = {"NV", "N/V", "ABSTAIN", "ABSTENTION", "ABSENT", "EXCUSED", "PRESENT", "—", "", "NA", "N/A",
                 "DID NOT VOTE", "DIDN'T VOTE"}

def _cell_to_yn(cell) -> str:
    """Map arbitrary cell text to 'Y' | 'N' | 'NV'."""
    if cell is None or (isinstance(cell, float) and pd.isna(cell)): 
        return "NV"
    U = str(cell).strip().upper()
    if U.startswith("PRO-") or U.startswith("PRO "): return "Y"
    if U.startswith("ANTI-") or U.startswith("AGAINST"): return "N"
    if U in NOVOTE_TOKENS: return "NV"
    if U in YES_TOKENS: return "Y"
    if U in NO_TOKENS:  return "N"
    return "NV"

# ---------- public loaders ----------
def load_votes() -> Tuple[Dict[str, List[str]], Dict[str, dict], List[dict]]:
    """Return (reps_by_district, rep_info, issues_list)."""
    votes_src = os.environ.get("NHRF_VOTES_SRC") or VOTES_CSV
    if not (str(votes_src).lower().startswith("http") or os.path.exists(votes_src)):
        raise FileNotFoundError(f"Missing votes CSV: {votes_src}")
    df = _df_from_source(votes_src)

    # discover columns
    name_col  = next(c for c in df.columns if c.lower() in ("name", "rep", "representative"))
    dist_col  = next(c for c in df.columns if "district" in c.lower())
    party_col = next(c for c in df.columns if "party" in c.lower())

    # issues drive labels/links and inversion
    issues = _load_issues()
    if not issues:
        # fallback: derive from CSV headers
        raw_cols = [c for c in df.columns if c not in (name_col, dist_col, party_col, "openstates_person_id")]
        def slugify(x): return re.sub(r"[^a-z0-9]+", "_", x.lower()).strip("_")
        issues = [IssueDef(slug=slugify(c), csv_header=c, label=c, bill="", bill_url="", support_when="Y") for c in raw_cols]

    reps_by_district: Dict[str, List[str]] = {}
    rep_info: Dict[str, dict] = {}

    for _, row in df.iterrows():
        rid = str(row.get("openstates_person_id") or f"{row[name_col]}|{row[dist_col]}")
        nm  = str(row[name_col]).strip()
        dist= str(row[dist_col]).strip()
        par = str(row[party_col]).strip()

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
                "raw": "" if (raw is None or (isinstance(raw, float) and pd.isna(raw))) else str(raw).strip(),
                "vote": yn
            }

        rep_info[rid] = {"id": rid, "name": nm, "party": par, "district": dist, "votes": votes}
        for key in district_key_variants(dist):
            reps_by_district.setdefault(key, []).append(rid)

    issues_out = [dict(slug=it.slug, label=it.label, bill=it.bill, bill_url=it.bill_url,
                       support_when=it.support_when, csv_header=it.csv_header) for it in issues]
    return reps_by_district, rep_info, issues_out

class _GeoIndex:
    """Lightweight holder so len(GEO.items) works; no spatial ops here."""
    def __init__(self, path: str):
        self.items: List[dict] = []
        if path and os.
