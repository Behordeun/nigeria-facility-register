"""
Fetch and place the raw inputs listed in data/sources.yml into data/raw/.

The GRID3 geopackages sit behind an AWS WAF JavaScript challenge on HDX, so a
plain scripted GET returns a 202 challenge page, not the file. This script does
not try to defeat that. Download the two .gpkg resources in a browser (the WAF
passes for a human), then hand the downloaded files to --place, which validates
them and places a copy into data/raw/ atomically.

Why --place rather than copying the file in by hand: a geopackage copied or
downloaded straight to its final path in data/raw/ is visible at that path while
it is still being written, and the Dagster new-vintage sensor or a manual load
can read it half-written. --place copies to a temp name in the same directory,
validates THAT copy (so bytes that changed mid-read cannot slip through), and
os.replace()s it onto the final name, which is atomic within a filesystem, so
the file at the final path is always complete and valid. Validation opens the
file with the GPKG driver and reads one feature, so a truncated file, a WAF
challenge page, a non-GeoPackage under a .gpkg name, or an empty geopackage is
rejected before it lands. Pass both files to one --place so they land as a
consistent set rather than one-new-one-old. The source downloads are left where
they are; this places a copy.

Usage:
    python scripts/fetch_sources.py                      # check what is present
    python scripts/fetch_sources.py --verify             # fail if missing/invalid
    python scripts/fetch_sources.py --place ~/Downloads/*.gpkg    # place both as a set
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
    """Confirm the file is a complete GeoPackage whose feature data is readable.

    Checks, in order: the file exists and clears a size floor (a fast reject for
    a WAF challenge page); it opens with the GPKG driver specifically, so a
    different OGR-readable format under a .gpkg name is rejected; it has at least
    one layer; and at least one feature actually reads back. The last step is the
    point: list_layers reads only metadata, so a file whose layer table survives
    but whose feature data is truncated would pass a metadata-only check and then
    break the loader. Reading one feature confirms the feature data is there and
    readable without scanning the whole file. Returns (ok, reason).
    """
    if not path.is_file():
        return False, "not a file"
    size = path.stat().st_size
    if size < MIN_GPKG_BYTES:
        return False, f"too small ({size} bytes); likely truncated or a WAF challenge page"
    try:
        import pyogrio

        info = pyogrio.read_info(str(path))
    except Exception as exc:  # noqa: BLE001 - report any open failure as invalid
        return False, f"not a readable geopackage: {exc}"

    driver = (info or {}).get("driver")
    if driver != "GPKG":
        return False, f"not a GeoPackage (OGR driver is {driver!r}, expected 'GPKG')"
    if not (info or {}).get("features"):
        return False, "geopackage has no features"

    # Read a single feature so a truncated feature table (readable metadata, bad
    # data) is caught, not just an empty one. max_features=1 avoids a full scan.
    try:
        gdf = pyogrio.read_dataframe(str(path), max_features=1)
    except Exception as exc:  # noqa: BLE001
        return False, f"feature data is not readable: {exc}"
    if len(gdf) < 1:
        return False, "no feature could be read"
    return True, ""


def _stage(download: Path) -> tuple[str, Path]:
    """Copy one validated download to a temp file beside data/raw/ and validate
    the COPY, returning (expected_name, temp_path).

    Validating the copy, not the source, is what makes the placement safe
    against a download that changes while it is being read: whatever bytes
    actually landed in the temp file are the bytes that get promoted, and they
    are re-checked here. The temp file is set to mode 0o640 (owner read/write,
    group read, no world access) so a loader or sensor running as a different
    account in the same group can read it once promoted, without exposing the
    raw geopackages to every account; mkstemp alone would leave it owner-only.
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
    # Temp file in the SAME directory as the destination, so os.replace is a
    # same-filesystem atomic rename (a cross-filesystem replace is not atomic).
    # Suffix .part.gpkg: the trailing .gpkg keeps pyogrio from warning about a
    # non-conformant extension while validating the staged copy, and .part marks
    # it as an incomplete staging file. The leading dot hides it.
    fd, tmp_name = tempfile.mkstemp(dir=str(RAW), prefix=f".{name}.", suffix=".part.gpkg")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_bytes(download.read_bytes())
        # Set the mode to exactly 0o640: owner read/write, group read, no world
        # access. os.chmod sets the bits directly and is NOT subject to the
        # umask (umask only masks the mode at creation), so group-read is
        # guaranteed here regardless of how restrictive the process umask is.
        # Masking with the umask was both wrong (a umask like 0o040 would strip
        # the group-read bit this fix exists to grant) and unnecessary. A
        # sensor/loader account in the same group can read the promoted file;
        # the raw geopackages are not world-readable.
        os.chmod(tmp, 0o640)
        ok, reason = validate_geopackage(tmp)
        if not ok:
            raise SystemExit(f"refusing to place {name}: staged copy invalid ({reason})")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return name, tmp


def place_all(downloads: list[Path]) -> list[Path]:
    """Validate every download, stage each as a validated temp copy, then
    promote them all with atomic renames.

    Promotion happens only after EVERY input has been validated and staged, so a
    set of inputs lands as a consistent group: the refresh never sees one new
    file beside one old one because a validation failure on the second aborts
    before the first is promoted. Within the promote step each file is an atomic
    os.replace; the brief window between the two replaces is unavoidable without
    a directory-level swap, but both files are already staged and valid, so the
    window is a rename apart, not a download apart. The source downloads are left
    in place (this places a copy; it does not delete the user's file).
    """
    staged: list[tuple[str, Path]] = []
    try:
        for download in downloads:
            staged.append(_stage(download))
    except BaseException:
        for _, tmp in staged:
            tmp.unlink(missing_ok=True)
        raise

    def _promote(name: str, tmp: Path) -> Path:
        dest = RAW / name
        os.replace(tmp, dest)
        print(f"placed {name} -> {dest} ({dest.stat().st_size // 1_000_000} MB)")
        return dest

    placed = [_promote(name, tmp) for name, tmp in staged]
    return placed


def place(download: Path) -> Path:
    """Place a single validated download; thin wrapper over place_all."""
    return place_all([download])[0]


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
    ap.add_argument("--place", metavar="FILE", nargs="+",
                    help="validate downloaded geopackage(s) and place them into data/raw/ "
                         "atomically; pass both files together to land them as a consistent set")
    args = ap.parse_args()

    if args.place:
        place_all([Path(f) for f in args.place])
        return
    check(args.verify)


if __name__ == "__main__":
    main()
