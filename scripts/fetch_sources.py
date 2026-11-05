"""
Fetch and place the raw inputs listed in data/sources.yml into data/raw/.

The GRID3 geopackages sit behind an AWS WAF JavaScript challenge on HDX, so a
plain scripted GET returns a 202 challenge page, not the file. This script does
not try to defeat that. Download the two .gpkg resources in a browser (the WAF
passes for a human), then hand each downloaded file to --place, which validates
it and moves it into data/raw/ atomically.

Why --place rather than copying the file in by hand: a geopackage copied or
downloaded straight to its final path in data/raw/ is visible at that path while
it is still being written, and the Dagster new-vintage sensor or a manual load
can read it half-written. --place writes to a temp name in the same directory
and os.replace()s it onto the final name, which is atomic within a filesystem,
so the file at the final path is always complete. It also opens the file first
and confirms it is a real geopackage with readable layers, so a truncated or
wrong download is rejected before it lands.

Usage:
    python scripts/fetch_sources.py                      # check what is present
    python scripts/fetch_sources.py --verify             # fail if missing/invalid
    python scripts/fetch_sources.py --place ~/Downloads/nga_health_facilities_v2_0.gpkg
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
# A fast reject for an obviously-truncated file or a WAF challenge HTML page,
# below any real geopackage (the committed two-state sample is ~0.5 MB; the full
# national files are tens of MB). The authoritative check is the layer open
# below; this only avoids handing a few bytes of HTML to the gpkg reader.
MIN_GPKG_BYTES = 64_000

EXPECTED = {
    "nga_health_facilities_v2_0.gpkg":
        "https://data.humdata.org/dataset/grid3-nga-health-facilities-v2-0",
    "grid3_nga_health_facilities_v3_0.gpkg":
        "https://data.humdata.org/dataset/grid3-nga-health-facilities-v3-0",
}


def validate_geopackage(path: Path) -> tuple[bool, str]:
    """Confirm the file is a complete, openable geopackage with at least one
    readable layer and one feature.

    Opening the layer list (pyogrio.list_layers) reads the gpkg SQLite header
    and the gpkg_contents table, not every feature, so it is cheap but catches a
    truncated or non-geopackage file that a size check would pass. Returns
    (ok, reason); reason is empty on success.
    """
    if not path.is_file():
        return False, "not a file"
    size = path.stat().st_size
    if size < MIN_GPKG_BYTES:
        return False, f"too small ({size} bytes); likely truncated or a WAF challenge page"
    try:
        import pyogrio

        layers = pyogrio.list_layers(str(path))
    except Exception as exc:  # noqa: BLE001 - report any open failure as invalid
        return False, f"not a readable geopackage: {exc}"
    if layers is None or len(layers) == 0:
        return False, "geopackage has no layers"
    return True, ""


def place(download: Path) -> Path:
    """Validate a downloaded geopackage and move it into data/raw/ atomically.

    The destination name is taken from the download's own file name and must be
    one of the expected inputs, so a mis-named download is rejected rather than
    landing under a name the loader does not read. The move is temp-name +
    os.replace within data/raw/, so the final path only ever holds a complete
    file.
    """
    download = download.expanduser()
    name = download.name
    if name not in EXPECTED:
        raise SystemExit(
            f"unexpected file name {name!r}; expected one of: {', '.join(sorted(EXPECTED))}"
        )
    ok, reason = validate_geopackage(download)
    if not ok:
        raise SystemExit(f"refusing to place {name}: {reason}")

    RAW.mkdir(parents=True, exist_ok=True)
    dest = RAW / name
    # Temp file in the SAME directory as dest, so os.replace is a same-filesystem
    # atomic rename (a cross-filesystem replace is not atomic).
    fd, tmp_name = tempfile.mkstemp(dir=str(RAW), prefix=f".{name}.", suffix=".part")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        # Copy the bytes into the temp file, then atomically swap it into place.
        tmp.write_bytes(download.read_bytes())
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    print(f"placed {name} -> {dest} ({dest.stat().st_size // 1_000_000} MB)")
    return dest


def check(verify: bool) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    problems = []
    for name, page in EXPECTED.items():
        p = RAW / name
        ok, reason = validate_geopackage(p)
        if ok:
            print(f"present: {name} ({p.stat().st_size // 1_000_000} MB)")
        else:
            problems.append(name)
            label = "MISSING" if not p.exists() else "INVALID"
            print(f"{label}: {name} ({reason})")
            print(f"         download the .gpkg resource from {page}")
            print("         then: python scripts/fetch_sources.py --place <downloaded file>")

    if problems and verify:
        print(f"\n{len(problems)} raw file(s) missing or invalid. HDX serves these behind "
              "an AWS WAF that blocks scripted download; fetch them in a browser and --place them.")
        sys.exit(1)
    if not problems:
        print("\nall raw inputs present and valid.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true",
                    help="exit non-zero if any required raw file is missing or invalid")
    ap.add_argument("--place", metavar="FILE",
                    help="validate a downloaded geopackage and move it into data/raw/ atomically")
    args = ap.parse_args()

    if args.place:
        place(Path(args.place))
        return
    check(args.verify)


if __name__ == "__main__":
    main()
