"""
Build the derived per-LGA enrichment inputs from the Geospatial monorepo files.

The enrichment phase layers three LGA-grain inputs onto the register: the ESPEN
NTD endemicity suite, flood-event exposure, and WorldPop births. Their source
files live in the Geospatial monorepo. This script reads them READ-ONLY and
writes small derived tables into data/derived/, which are committed and loaded
by nfr.load_sources. The monorepo originals are never written, moved or
regenerated.

What each output is, and why it is shaped this way:

- ntd_espen_lga.csv: one row per (LGA, disease) for the five diseases in the
  ESPEN long table (STH, schistosomiasis, LF, onchocerciasis, Loa loa). Its
  source of record is now ESPEN's public download route, via
  scripts/build_espen_lga.py, which reproduces the same table; this path from
  the monorepo's long table is kept for comparison. ESPEN is
  CC BY-NC-SA 3.0 IGO, so the raw IU table is NOT committed; only this summary
  is. The long table repeats STH and schistosomiasis once per MDA target
  population (SAC, Total, Adults, PreSAC) with the same endemicity, so it is
  first reduced to one label per (IU, disease, year); conflicting labels for one
  IU-year abort the build rather than being resolved by row order. The summary
  keeps the latest year's label as published and, separately, the latest label
  that is not an "Unknown ..." status, so a recent unknown never hides an older
  measured status and is never read as a measured low.
- flood_event_affected_lga.csv: one row per (LGA, flood event). The 2022 event
  is state-resolution (the state's flag repeated to each LGA) and the 2024 event
  LGA-resolution; the resolution column carries that so neither is read as the
  other. affected is a 0/1 exposure flag, not a severity.
- births_lga.csv: WorldPop 2015 annual births per LGA, keyed on shapeID.

Every output must cover exactly the 774 frame LGAs (on shapeID or the
normalised (state, canonical_lga) key) before anything is written. Writes go
through a temp file and an atomic rename.

Usage:
    python scripts/build_enrichment_sources.py \
        --espen /path/to/Geospatial/espen_ntd/data/espen_long.csv \
        --flood-affected /path/to/Geospatial/flood_health/data/lga_flood_event_affected.csv \
        --births /path/to/Geospatial/sickle_cell/data/lga_births.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from nfr.enrichment.espen import ESPEN_COLUMNS, ESPEN_DISEASES, derive_espen, is_unknown  # noqa: E402,F401
from nfr.keys import norm_lga, norm_state  # noqa: E402
from nfr.files import write_csv_atomic  # noqa: E402

FRAME_INDEX = ROOT / "data" / "frame" / "nigeria_master_lga_index.csv"
DEFAULT_OUT = ROOT / "data" / "derived"
EXPECTED_LGAS = 774
FLOOD_RESOLUTIONS = {"LGA", "state"}

FLOOD_COLUMNS = ["state", "canonical_lga", "event_year", "affected", "resolution"]
BIRTHS_COLUMNS = ["shapeID", "shapeName", "births"]


def _key(state: pd.Series, lga: pd.Series) -> pd.Series:
    return state.map(norm_state) + "|" + lga.map(norm_lga)


def frame_keys(frame: pd.DataFrame) -> set[str]:
    return set(_key(frame["state"], frame["canonical_lga"]))


def check_lga_keys(df: pd.DataFrame, frame: pd.DataFrame, name: str) -> None:
    """Fail unless df covers exactly the 774 frame LGAs on (state, canonical_lga)."""
    got = set(_key(df["state"], df["canonical_lga"]))
    want = frame_keys(frame)
    if got != want:
        raise SystemExit(
            f"{name}: LGA keys do not match the frame "
            f"({len(got - want)} not in frame, {len(want - got)} frame LGAs missing)"
        )


def derive_flood_affected(raw: pd.DataFrame) -> pd.DataFrame:
    if missing := set(FLOOD_COLUMNS) - set(raw.columns):
        raise SystemExit(f"flood-affected: missing columns {sorted(missing)}")
    out = raw[FLOOD_COLUMNS].copy()
    if out["affected"].isna().any() or not set(out["affected"]).issubset({0, 1}):
        raise SystemExit("flood-affected: affected must be a non-null 0/1 flag")
    if not set(out["resolution"]).issubset(FLOOD_RESOLUTIONS):
        raise SystemExit(f"flood-affected: resolution outside {sorted(FLOOD_RESOLUTIONS)}")
    if out.duplicated(["state", "canonical_lga", "event_year"]).any():
        raise SystemExit("flood-affected: duplicate (LGA, event_year) rows")
    if (out.groupby("event_year")["resolution"].nunique() > 1).any():
        raise SystemExit("flood-affected: an event mixes LGA and state resolution")
    out["affected"] = out["affected"].astype(int)
    return out.sort_values(["event_year", "state", "canonical_lga"]).reset_index(drop=True)


def derive_births(raw: pd.DataFrame, frame: pd.DataFrame) -> pd.DataFrame:
    if missing := set(BIRTHS_COLUMNS) - set(raw.columns):
        raise SystemExit(f"births: missing columns {sorted(missing)}")
    out = raw[BIRTHS_COLUMNS].copy()
    if out["shapeID"].duplicated().any():
        raise SystemExit("births: duplicate shapeID")
    if set(out["shapeID"]) != set(frame["shapeID"]):
        raise SystemExit("births: shapeIDs do not match the 774-LGA frame exactly")
    if out["births"].isna().any() or (out["births"] <= 0).any():
        raise SystemExit("births: every LGA needs a positive births figure")
    return out.sort_values("shapeID").reset_index(drop=True)


def write_atomic(df: pd.DataFrame, out: Path) -> None:
    write_csv_atomic(df, out)
    print(f"wrote {out} ({len(df)} rows)")


def build(espen: Path, flood: Path, births: Path, out_dir: Path) -> None:
    sources = [p.resolve() for p in (espen, flood, births)]
    out_dir = out_dir.resolve()
    # The originals must never be written: refuse an output directory that holds
    # any of them at any depth, so a mistyped --out cannot write into the
    # monorepo tree the sources live in.
    if any(out_dir in src.parents for src in sources):
        raise SystemExit(f"refusing to write into a source directory: {out_dir}")

    frame = pd.read_csv(FRAME_INDEX)
    if len(frame_keys(frame)) != EXPECTED_LGAS:
        raise SystemExit("frame does not carry 774 distinct LGA keys")

    espen_df = derive_espen(pd.read_csv(sources[0], low_memory=False))
    check_lga_keys(espen_df, frame, "espen")
    if len(espen_df) != EXPECTED_LGAS * len(ESPEN_DISEASES):
        raise SystemExit(f"espen: expected {EXPECTED_LGAS} LGAs x 5 diseases, got {len(espen_df)}")

    flood_df = derive_flood_affected(pd.read_csv(sources[1]))
    for year, part in flood_df.groupby("event_year"):
        check_lga_keys(part, frame, f"flood-affected {year}")

    births_df = derive_births(pd.read_csv(sources[2]), frame)

    # Everything validated before any file is written.
    write_atomic(espen_df, out_dir / "ntd_espen_lga.csv")
    write_atomic(flood_df, out_dir / "flood_event_affected_lga.csv")
    write_atomic(births_df, out_dir / "births_lga.csv")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--espen", required=True, help="Geospatial espen_ntd/data/espen_long.csv")
    ap.add_argument("--flood-affected", required=True,
                    help="Geospatial flood_health/data/lga_flood_event_affected.csv")
    ap.add_argument("--births", required=True, help="Geospatial sickle_cell/data/lga_births.csv")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()
    paths = [Path(p).expanduser() for p in (args.espen, args.flood_affected, args.births)]
    for p in paths:
        if not p.is_file():
            print(f"source not found: {p}", file=sys.stderr)
            sys.exit(1)
    build(*paths, Path(args.out))


if __name__ == "__main__":
    main()
