# backend/loader.py — drop-in (only change: NV -> raw == "")
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
    """Return (rep
