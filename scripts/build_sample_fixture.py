"""
Build the committed sample fixture from the full source geopackages.

CI cannot fetch the real GRID3 geopackages (HDX serves them behind a WAF), so
without a fixture CI can only parse the dbt project, never build it. This script
extracts a small, faithful subset into data/sample/ so the whole transform runs
in CI and the enforced data contract is actually proven, not just declared.

The subset is chosen to exercise both authority paths:
  - Bayelsa: covered by v3.0, so it tests the current (v3) path.
  - Ekiti: NOT covered by v3.0, so it tests the stale (v2) fallback.
Both states also carry ESPEN STH data and a few LGA alias variants, so the
crosswalk join is exercised too.

This is a developer tool, run once when the source vintages change. The real
pipeline still runs on the full data; the fixture is for CI and local smoke
tests. Point it at wherever the full geopackages live.

Usage:
    python scripts/build_sample_fixture.py --src /path/to/dir/with/gpkgs
"""
from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "sample"

# States chosen so the fixture exercises v3-covered and v2-only paths.
V3_STATE = "Bayelsa"      # v3.0 covers it: state_standard
V2_STATE = "Ekiti"        # v3.0 does not cover it: lives only in v2.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="dir holding the full v2 and v3 geopackages")
    args = ap.parse_args()
    src = Path(args.src)
    v2_path = src / "nga_health_facilities_v2_0.gpkg"
    v3_path = src / "grid3_nga_health_facilities_v3_0.gpkg"
    for p in (v2_path, v3_path):
        if not p.exists():
            raise SystemExit(f"missing source geopackage: {p}")

    OUT.mkdir(parents=True, exist_ok=True)

    # v2.0: keep both states (v2 is national, so it has Bayelsa and Ekiti).
    v2 = gpd.read_file(v2_path)
    v2_sample = v2[v2["state"].astype(str).str.strip().isin([V3_STATE, V2_STATE])].copy()
    v2_out = OUT / "nga_health_facilities_v2_0.gpkg"
    v2_sample.to_file(v2_out, driver="GPKG")
    print(f"v2 sample: {len(v2_sample)} rows ({V3_STATE} + {V2_STATE}) -> {v2_out}")

    # v3.0: keep only Bayelsa (Ekiti is not in v3, which is the point).
    v3 = gpd.read_file(v3_path)
    v3_sample = v3[v3["state_standard"].astype(str).str.strip() == V3_STATE].copy()
    v3_out = OUT / "grid3_nga_health_facilities_v3_0.gpkg"
    v3_sample.to_file(v3_out, driver="GPKG")
    print(f"v3 sample: {len(v3_sample)} rows ({V3_STATE} only) -> {v3_out}")

    print("\nSample fixture built. Commit data/sample/ so CI can build the transform.")
    print("The authority layer should show Bayelsa as current (v3) and Ekiti as stale (v2).")


if __name__ == "__main__":
    main()
