import os, re, requests

GEOCODER = os.getenv("GEOCODER", "census").lower()

def _clean(addr: str) -> str:
    return re.sub(r"\s+", " ", addr).strip()

def geocode_address(addr: str):
    """Return (lon, lat) or (None, None)."""
    if GEOCODER == "none":
        return (None, None)
    a = _clean(addr)
    url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
    params = {"address": f"{a}, NH", "benchmark": "Public_AR_Current", "format": "json"}
    try:
        r = requests.get(url, params=params, timeout=6)
        r.raise_for_status()
        m = r.json().get("result", {}).get("addressMatches", [])
        if not m: return (None, None)
        c = m[0]["coordinates"]
        return (float(c["x"]), float(c["y"]))
    except Exception:
        return (None, None)
