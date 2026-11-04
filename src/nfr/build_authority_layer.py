"""
Build the version-aware authoritative facility layer for Nigeria.

GRID3 v3.0 covers only 24 states and supersedes v2.0 there; v2.0 is the only
national coverage. This builds one current-best layer by switching authority
per state: v3.0 where it exists, v2.0 everywhere else. Each output row carries
the source version and the authority reason, so the layer is honest about which
states are current and which are stale.

This is the project's core artifact: a single as-of-now register that tracks
the per-state version boundary instead of silently mixing two vintages.

Run:
    flood_health/.venv/bin/python facility_registry/build_authority_layer.py

Outputs (facility_registry/output/):
    authority_facilities.csv   one row per facility, with source_version + reason
    authority_coverage.csv     per-state: which version is authoritative, counts
    authority_report.txt
"""
import csv
from collections import defaultdict
from pathlib import Path

import geopandas as gpd

HERE = Path(__file__).resolve().parent
FRAME = HERE.parent / "_shared_frame"
V2 = FRAME / "nga_health_facilities_v2_0.gpkg"
V3 = FRAME / "grid3_nga_health_facilities_v3_0.gpkg"
OUT = HERE / "output"

STATE_FIX = {"FCT, Abuja": "FCT", "Fct": "FCT", "Federal Capital Territory": "FCT",
             "Nassarawa": "Nasarawa"}
V3_DATE = "2026-08-13"
V2_DATE = "2024-10-31"


def norm_state(s):
    s = (s or "").strip()
    return STATE_FIX.get(s, s)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    v2 = gpd.read_file(V2)
    v3 = gpd.read_file(V3)
    v2["state_n"] = v2["state"].map(norm_state)
    v3["state_n"] = v3["state_standard"].map(norm_state)

    v3_states = set(v3["state_n"].dropna())
    all_states = sorted(set(v2["state_n"].dropna()) | v3_states)

    rows = []
    # v3 is authoritative for its 24 states.
    for _, r in v3.iterrows():
        st = r["state_n"]
        if st not in v3_states:
            continue
        rows.append({
            "facility_id": r.get("unique_id"),
            "state": st,
            "lga": r.get("lga_standard"),
            "facility_name": r.get("facility_name"),
            "facility_level": r.get("facility_level"),
            "functional": r.get("functional"),
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
            "source_version": "v3.0",
            "source_date": V3_DATE,
            "authority_reason": "v3.0 covers this state (current)",
        })
    # v2 is authoritative for every state v3 does NOT cover.
    for _, r in v2.iterrows():
        st = r["state_n"]
        if st in v3_states:
            continue  # v3 wins here
        rows.append({
            "facility_id": r.get("globalid"),
            "state": st,
            "lga": r.get("lga"),
            "facility_name": r.get("facility_name"),
            "facility_level": r.get("facility_level"),
            "functional": "",  # v2 has no functional field
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
            "source_version": "v2.0",
            "source_date": V2_DATE,
            "authority_reason": "v3.0 does not yet cover this state (stale)",
        })

    with open(OUT / "authority_facilities.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # Coverage ledger, per state.
    cov = []
    counts = defaultdict(int)
    for r in rows:
        counts[(r["state"], r["source_version"])] += 1
    for st in all_states:
        if st in v3_states:
            cov.append({"state": st, "authoritative_version": "v3.0",
                        "source_date": V3_DATE, "facilities": counts[(st, "v3.0")],
                        "status": "current"})
        else:
            cov.append({"state": st, "authoritative_version": "v2.0",
                        "source_date": V2_DATE, "facilities": counts[(st, "v2.0")],
                        "status": "stale-awaiting-v3"})
    with open(OUT / "authority_coverage.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(cov[0].keys()))
        w.writeheader()
        w.writerows(cov)

    n_v3 = sum(1 for r in rows if r["source_version"] == "v3.0")
    n_v2 = len(rows) - n_v3
    stale_states = [c["state"] for c in cov if c["status"].startswith("stale")]
    L = []
    L.append("Version-aware authoritative facility layer")
    L.append("=" * 50)
    L.append(f"Total facilities:        {len(rows)}")
    L.append(f"  from v3.0 (current):   {n_v3}  across {len(v3_states)} states")
    L.append(f"  from v2.0 (stale):     {n_v2}  across {len(stale_states)} states")
    L.append("")
    L.append(f"States still on v2.0 (awaiting v3.0 coverage): {len(stale_states)}")
    L.append("  " + ", ".join(stale_states))
    L.append("")
    L.append("Each row carries source_version + source_date + authority_reason, so a")
    L.append("consumer always knows whether a state's data is current or stale. When")
    L.append("v3.1 adds states, only authority_coverage flips; the switch is declarative.")
    (OUT / "authority_report.txt").write_text("\n".join(L))
    print("\n".join(L))


if __name__ == "__main__":
    main()
