#!/usr/bin/env python3
# Flip Pro/Anti ONLY for HB1/HB2 columns in backend/data/house_key_votes.csv
# Creates backend/data/house_key_votes.csv.bak as a backup.

import csv, shutil, sys, pathlib

CSV_PATH = pathlib.Path("backend/data/house_key_votes.csv")
COLUMNS_TO_FLIP = [
    "HB1 - State Funding Bill",
    "HB2 - Budget Trailer (All the Policy Stuff)",
]

def flip_cell(s: str) -> str:
    if s is None:
        return s
    v = s.strip()
    low = v.lower()
    # Swap only leading Pro-/Anti- labels; leave NV/blank/Yea/No etc. unchanged
    if low.startswith("pro"):
        return "Anti" + v[3:]
    if low.startswith("anti"):
        return "Pro" + v[4:]
    return s

def main():
    if not CSV_PATH.exists():
        print(f"ERROR: {CSV_PATH} not found", file=sys.stderr)
        sys.exit(1)

    bak = CSV_PATH.with_suffix(".csv.bak")
    shutil.copyfile(CSV_PATH, bak)

    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        headers = rdr.fieldnames or []
        rows = list(rdr)

    for r in rows:
        for col in COLUMNS_TO_FLIP:
            if col in r and r[col] is not None:
                r[col] = flip_cell(r[col])

    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerows(rows)

    print("Done. Flipped Pro/Anti in:", ", ".join(COLUMNS_TO_FLIP))
    print("Backup saved to:", bak)

if __name__ == "__main__":
    main()
