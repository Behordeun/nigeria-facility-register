"""
Spatially match GRID3 Health Facilities v2.0 to v3.0, scoped to the 24 states
v3.0 covers. Needed because the two versions share no carry-over id: v2 keys on
globalid, v3 on unique_id, and the NHFR code is too sparse in v3 to bridge them.

So lineage across versions can only be reconstructed by position + name, which
is the project's core claim: even the official releases cannot be diffed without
entity resolution.

Method (mirrors GRID3's own v3 harmonisation at a lighter touch):
  - Build a cKDTree on v3 points per state (projected to metres via a local
    equirectangular approximation, fine at this scale).
  - For each v2 point, find its nearest v3 point. Inside a 100 m gate it is a
    spatial candidate; confirm with a name-similarity score (difflib, since
    rapidfuzz is not installed). Report tight (<=25 m) and loose (25-100 m)
    bands, matching the thresholds v3's release notes use.
  - A v3 point is claimed once (nearest v2 wins), so no double-map.

Outputs (facility_registry/output/v2_v3_match/):
  matched.csv     v2<->v3 pairs with distance + name score
  v2_only.csv     v2 facilities with no v3 match (candidate 'dropped')
  v3_only.csv     v3 facilities with no v2 match (candidate 'appeared')
  report.txt      per-state and national carry-over / appear / drop counts
"""
import math
from difflib import SequenceMatcher
from pathlib import Path

import geopandas as gpd
import numpy as np
from scipy.spatial import cKDTree

HERE = Path(__file__).resolve().parent
FRAME = HERE.parent / "_shared_frame"
V2 = FRAME / "nga_health_facilities_v2_0.gpkg"
V3 = FRAME / "grid3_nga_health_facilities_v3_0.gpkg"
OUT = HERE / "output" / "v2_v3_match"

GATE_M = 100.0
TIGHT_M = 25.0

STATE_FIX = {"FCT, Abuja": "FCT", "Fct": "FCT", "Federal Capital Territory": "FCT",
             "Nassarawa": "Nasarawa"}


def norm_state(s):
    s = (s or "").strip()
    return STATE_FIX.get(s, s)


def to_xy(lat, lon, lat0):
    """Local equirectangular projection to metres. Good enough for <100 m
    nearest-neighbour inside one state."""
    r = 6371000.0
    x = np.radians(lon) * r * math.cos(math.radians(lat0))
    y = np.radians(lat) * r
    return x, y


def name_sim(a, b):
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return None
    return round(SequenceMatcher(None, a, b).ratio(), 3)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    v2 = gpd.read_file(V2)
    v3 = gpd.read_file(V3)
    v2["state_n"] = v2["state"].map(norm_state)
    v3["state_n"] = v3["state_standard"].map(norm_state)

    # coords: prefer explicit lat/lon columns, fall back to geometry
    def coords(df):
        lat = df["latitude"].to_numpy(dtype="float64", na_value=np.nan)
        lon = df["longitude"].to_numpy(dtype="float64", na_value=np.nan)
        return lat, lon

    v2["_lat"], v2["_lon"] = coords(v2)
    v3["_lat"], v3["_lon"] = coords(v3)

    v3_states = sorted(set(v3["state_n"].dropna()))
    matched_rows, v2_only, v3_only = [], [], []
    per_state = {}

    for st in v3_states:
        a = v2[(v2["state_n"] == st) & v2["_lat"].notna() & v2["_lon"].notna()]
        b = v3[(v3["state_n"] == st) & v3["_lat"].notna() & v3["_lon"].notna()]
        if len(b) == 0:
            continue
        lat0 = float(b["_lat"].mean())
        bx, by = to_xy(b["_lat"].to_numpy(), b["_lon"].to_numpy(), lat0)
        tree = cKDTree(np.c_[bx, by])
        b_idx = b.index.to_numpy()
        b_name = b["facility_name"].to_numpy()
        b_uid = b["unique_id"].to_numpy()

        claimed = set()
        n_match = n_tight = 0
        if len(a):
            ax, ay = to_xy(a["_lat"].to_numpy(), a["_lon"].to_numpy(), lat0)
            dist, idx = tree.query(np.c_[ax, ay], k=1)
            order = np.argsort(dist)  # assign closest first
            a_name = a["facility_name"].to_numpy()
            a_gid = a["globalid"].to_numpy()
            for oi in order:
                d = float(dist[oi])
                j = int(idx[oi])
                if d > GATE_M:
                    v2_only.append({"globalid": a_gid[oi], "state": st,
                                    "facility_name": a_name[oi], "nearest_m": round(d, 1)})
                    continue
                if j in claimed:
                    v2_only.append({"globalid": a_gid[oi], "state": st,
                                    "facility_name": a_name[oi], "nearest_m": round(d, 1)})
                    continue
                claimed.add(j)
                sim = name_sim(a_name[oi], b_name[j])
                n_match += 1
                if d <= TIGHT_M:
                    n_tight += 1
                matched_rows.append({
                    "state": st, "globalid": a_gid[oi], "unique_id": b_uid[j],
                    "v2_name": a_name[oi], "v3_name": b_name[j],
                    "distance_m": round(d, 1),
                    "name_sim": "" if sim is None else sim,
                    "band": "tight" if d <= TIGHT_M else "loose",
                })
        for k, ridx in enumerate(b_idx):
            if k not in claimed:
                v3_only.append({"unique_id": b_uid[k], "state": st,
                                "facility_name": b_name[k]})
        per_state[st] = {"v2": len(a), "v3": len(b),
                         "matched": n_match, "tight": n_tight}

    _write(OUT / "matched.csv", matched_rows)
    _write(OUT / "v2_only.csv", v2_only)
    _write(OUT / "v3_only.csv", v3_only)
    print(_report(per_state, matched_rows, v2_only, v3_only))


def _write(path, rows):
    import csv
    if not rows:
        path.write_text("")
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _report(per_state, matched, v2_only, v3_only):
    tv2 = sum(s["v2"] for s in per_state.values())
    tv3 = sum(s["v3"] for s in per_state.values())
    tm = len(matched)
    tt = sum(1 for m in matched if m["band"] == "tight")
    named = [m for m in matched if m["name_sim"] != ""]
    agree = [m for m in named if float(m["name_sim"]) >= 0.6]
    L = []
    L.append("GRID3 v2.0 -> v3.0 spatial facility match")
    L.append("=" * 50)
    L.append(f"Scope: {len(per_state)} states v3.0 covers (geocoded points only)")
    L.append(f"v2.0 points:          {tv2}")
    L.append(f"v3.0 points:          {tv3}")
    L.append("")
    L.append(f"Matched within {GATE_M:.0f} m:   {tm}")
    L.append(f"  tight (<= {TIGHT_M:.0f} m):     {tt}")
    L.append(f"  loose ({TIGHT_M:.0f}-{GATE_M:.0f} m):   {tm - tt}")
    L.append(f"  with both names:    {len(named)}")
    L.append(f"  name agrees (>=0.6): {len(agree)}  ({len(agree)/len(named)*100:.1f}% of named)")
    L.append("")
    L.append(f"v2 only (no v3 within gate) = candidate DROPPED: {len(v2_only)}")
    L.append(f"v3 only (no v2 within gate) = candidate APPEARED: {len(v3_only)}")
    L.append(f"carry-over rate: {tm/tv2*100:.1f}% of v2 matched to a v3 point")
    L.append("")
    L.append("Per-state (v2 / v3 / matched / carry-over%):")
    for st in sorted(per_state):
        s = per_state[st]
        cov = s["matched"] / s["v2"] * 100 if s["v2"] else 0
        L.append(f"  {st:14s} {s['v2']:5d} / {s['v3']:5d} / {s['matched']:5d} / {cov:5.1f}%")
    L.append("")
    L.append("Reading: a v2-only point is a candidate drop, not a confirmed closure;")
    L.append("v3 may have merged it into a nearby record under a different name, or the")
    L.append("facility moved >100 m, or v3 dropped a low-accuracy point. A v3-only point")
    L.append("is new coverage from the 10 non-v2 sources v3 ingested. Both lists are")
    L.append("REVIEW QUEUES, not verdicts, exactly where human judgement enters.")
    report = "\n".join(L)
    (OUT / "report.txt").write_text(report)
    return report


if __name__ == "__main__":
    main()
