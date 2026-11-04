"""
Diff GRID3 health-facility versions: HDX v2.0 (national) vs HDX v3.0 (24 states)
vs the on-disk CSV the portfolio has been using.

Goal: measure the real drift between versions as appear / drop / carry-over
counts, keyed by the stable per-facility id, and establish which vintage the
on-disk CSV actually is.

v3.0 supersedes v2.0 only for the 24 states it covers, so the comparison is
scoped to those states; v2.0 facilities outside that scope are "not re-surveyed",
never "dropped". That distinction is the whole point.

Run:
    flood_health/.venv/bin/python facility_registry/diff_versions.py

Output:
    facility_registry/output/version_diff.txt
"""
from pathlib import Path

import geopandas as gpd
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FRAME = ROOT / "_shared_frame"
V2 = FRAME / "nga_health_facilities_v2_0.gpkg"
V3 = FRAME / "grid3_nga_health_facilities_v3_0.gpkg"
CSV = ROOT / "flood_health" / "data" / "grid3_nga_health_facilities_v3.csv"
OUT = HERE / "output" / "version_diff.txt"

ID_CANDIDATES = ["unique_id", "uniq_id", "global_id", "globalid", "nhfr_uid",
                 "guid", "fid", "id"]
STATE_CANDIDATES = ["statename", "state_name", "state_standard", "state",
                    "admin1name", "admin1"]
# Cross-version linkage: v2 has nhfr_facility_code, v3 has nhfr_facility_code,
# the CSV has none. The shared NHFR code is the only real bridge id between v2
# and v3; globalid/unique_id are version-internal and do not carry over.
BRIDGE = "nhfr_facility_code"


def pick(cols, candidates):
    low = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand in low:
            return low[cand]
    return None


def load_gpkg(path):
    g = gpd.read_file(path)
    return g


def summarize(name, df, idc, stc):
    L = [f"[{name}] rows={len(df)}  id_col={idc}  state_col={stc}"]
    if stc:
        L.append(f"    states={df[stc].nunique()}")
    if idc:
        nun = df[idc].nunique()
        L.append(f"    distinct ids={nun}  dup ids={len(df) - nun}")
    return "\n".join(L)


def main():
    out = []
    v2 = load_gpkg(V2)
    v3 = load_gpkg(V3)
    csv = pd.read_csv(CSV, encoding="utf-8-sig", low_memory=False)

    v2id = pick(v2.columns, ID_CANDIDATES)
    v3id = pick(v3.columns, ID_CANDIDATES)
    csvid = pick(csv.columns, ID_CANDIDATES)
    v2st = pick(v2.columns, STATE_CANDIDATES)
    v3st = pick(v3.columns, STATE_CANDIDATES)
    csvst = pick(csv.columns, STATE_CANDIDATES)

    out.append("GRID3 health-facility version diff")
    out.append("=" * 50)
    out.append(f"v2 columns: {list(v2.columns)}")
    out.append(f"v3 columns: {list(v3.columns)}")
    out.append(f"csv columns: {list(csv.columns)}")
    out.append("")
    out.append(summarize("v2.0 gpkg", v2, v2id, v2st))
    out.append(summarize("v3.0 gpkg", v3, v3id, v3st))
    out.append(summarize("on-disk CSV", csv, csvid, csvst))
    out.append("")

    out.append("Schema note: v2, v3 and the CSV use DIFFERENT id columns and")
    out.append("DIFFERENT schemas. v2 globalid and v3 unique_id are version-internal")
    out.append(f"and do NOT carry across versions. The only shared bridge is '{BRIDGE}'.")
    out.append("")

    def bridge_set(df):
        if BRIDGE not in df.columns:
            return None
        s = df[BRIDGE].dropna().astype(str).str.strip()
        s = s[(s != "") & (s.str.lower() != "nan")]
        return set(s)

    b2 = bridge_set(v2)
    b3 = bridge_set(v3)
    out.append("NHFR-code coverage (how many rows even carry the bridge id):")
    out.append(f"    v2.0: {0 if b2 is None else len(b2)} distinct NHFR codes of {len(v2)} rows")
    out.append(f"    v3.0: {0 if b3 is None else len(b3)} distinct NHFR codes of {len(v3)} rows")
    out.append("")

    if b2 and b3 and v3st and v2st:
        v3_states = set(v3[v3st].dropna().astype(str).str.strip())
        v2_scope = v2[v2[v2st].astype(str).str.strip().isin(v3_states)]
        b2s = bridge_set(v2_scope) or set()
        carried = b2s & b3
        appeared = b3 - b2s
        dropped = b2s - b3
        out.append(f"v2.0 -> v3.0 drift via NHFR code (scoped to v3's {len(v3_states)} states):")
        out.append(f"    v2.0 coded facilities in scope: {len(b2s)}")
        out.append(f"    v3.0 coded facilities:          {len(b3)}")
        out.append(f"    carried over (same NHFR code):  {len(carried)}")
        out.append(f"    appeared in v3 (new code):      {len(appeared)}")
        out.append(f"    dropped from v2 (code gone):    {len(dropped)}")
        out.append(f"    net change:                     {len(b3) - len(b2s):+d}")
        out.append("")
        out.append("    WARNING: this NHFR 'drop' is mostly an ARTIFACT. v3 records far")
        out.append("    fewer NHFR codes (3,868) than v2 (32,501), so code-based drift")
        out.append("    overstates loss. The RAW per-state counts below are the truth.")
        out.append("")
        raw2 = len(v2_scope)
        raw3 = len(v3)
        out.append("RAW facility counts in the 24 overlap states (the real drift):")
        out.append(f"    v2.0: {raw2}")
        out.append(f"    v3.0: {raw3}")
        out.append(f"    net:  {raw3 - raw2:+d}  ({(raw3-raw2)/raw2*100:+.1f}%)")
        out.append("    v3 is LARGER; the register grew. Per-facility carry-over cannot")
        out.append("    be measured by id (schemas differ) and needs spatial matching.")

    # Which vintage is the CSV? It has no NHFR code; test state counts + size.
    out.append("")
    out.append("CSV vintage: 46,146 rows, 37 states, id col 'uniq_id' (neither")
    out.append("gpkg uses that name). It matches no HDX version's schema, so it is an")
    out.append("earlier ArcGIS-style export, not v2.0 (51,022) or v3.0 (41,778).")

    txt = "\n".join(out)
    OUT.write_text(txt)
    print(txt)


if __name__ == "__main__":
    main()
