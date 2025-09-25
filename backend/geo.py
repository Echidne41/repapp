# ELI5: small geocoding helpers.
import os, re, requests

GEOCODER = os.getenv("GEOCODER", "census").lower()

def _clean_addr(addr: str) -> str:
    return re.sub(r"\s+", " ", addr).strip()

def geocode_address(addr: str):
    """
    Returns (lon, lat) in WGS84 or (None, None) on failure.
    Uses US Census single-line endpoint for NH only.
    """
    if GEOCODER == "none":
        return (None, None)

    a = _clean_addr(addr)
    # Census single-line endpoint (free, no key). Filter to NH to avoid wrong states.
    url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
    params = {
        "address": f"{a}, NH",
        "benchmark": "Public_AR_Current",
        "format": "json",
    }
    try:
        r = requests.get(url, params=params, timeout=6)
        r.raise_for_status()
        js = r.json()
        matches = js.get("result", {}).get("addressMatches", [])
        if not matches:
            return (None, None)
        loc = matches[0]["coordinates"]
        return (float(loc["x"]), float(loc["y"]))
    except Exception:
        return (None, None)

