"""
The identity of a refresh's inputs: everything that decides what the register
contains.

The refresh fingerprints its inputs twice: the sensor fingerprints the live
inputs to decide whether to rebuild, and the run fingerprints its private
snapshot to record what it published. Both must cover the same things, or the
published fingerprint would understate what the register was built from. That
set is:

- the two GRID3 geopackages (data/raw/);
- every file in the LGA frame (data/frame/) and the derived enrichment tables
  (data/derived/), which the loader reads alongside the geopackages;
- the dbt project (models, macros, seeds, project files), whose SQL turns those
  inputs into the marts;
- the population year the build selects, which changes the published figures
  on 1 January with no file changing at all.

A change to any of them changes the fingerprint, so the sensor rebuilds and the
live register always records the full set it was built from. Pure and
path-injectable, so it is tested without Dagster.
"""
from __future__ import annotations

import csv
import hashlib
from datetime import date
from pathlib import Path

FRAME_SUBDIR = "frame"
DERIVED_SUBDIR = "derived"
POPULATION_TABLE = "access_population_lga.csv"
POPULATION_YEAR_COLUMN = "population_year"

# The parts of the dbt project that shape the marts. dbt's own tests gate a
# build but never change its output, so they are left out: editing a test does
# not republish an identical register.
DBT_PROJECT_FILES = ("dbt_project.yml", "packages.yml")
DBT_PROJECT_DIRS = ("models", "macros", "seeds")


def input_files(directory: Path) -> list[str]:
    """Names of the regular, non-hidden files directly in directory, sorted.

    A missing directory yields no names, so a fingerprint of an incomplete set
    still differs from the complete one rather than raising.
    """
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.is_file() and not p.name.startswith("."))


def select_population_year(population_table: Path, today: date) -> int:
    """The population year a build made today describes.

    The calendar year, capped to the years the population table carries, which
    is the same rule stg_access_population_lga applies when no year is pinned.
    Computing it here lets the refresh pin that year for dbt, so the year in
    the fingerprint is exactly the year built. Raises ValueError when the table
    has no year column or no rows.
    """
    with population_table.open(newline="") as f:
        reader = csv.DictReader(f)
        if POPULATION_YEAR_COLUMN not in (reader.fieldnames or []):
            raise ValueError(f"{population_table} has no {POPULATION_YEAR_COLUMN} column")
        years = {int(row[POPULATION_YEAR_COLUMN]) for row in reader}
    if not years:
        raise ValueError(f"{population_table} carries no {POPULATION_YEAR_COLUMN} values")
    return min(max(today.year, min(years)), max(years))


def dbt_project_digest(dbt_dir: Path) -> str:
    """A content hash of the dbt files that shape the marts.

    Each file contributes its path relative to dbt_dir and its bytes, in sorted
    order, so renaming, adding, removing or editing a model changes the digest.
    """
    h = hashlib.sha256()
    files = [dbt_dir / name for name in DBT_PROJECT_FILES if (dbt_dir / name).is_file()]
    for sub in DBT_PROJECT_DIRS:
        root = dbt_dir / sub
        if root.is_dir():
            files.extend(p for p in root.rglob("*") if p.is_file())
    for path in sorted(files, key=lambda p: p.relative_to(dbt_dir).as_posix()):
        h.update(path.relative_to(dbt_dir).as_posix().encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def _hash_confined(h, base: Path, directory: Path, names: list[str], label: str) -> None:
    """Hash each named file in directory, refusing any that resolves outside base.

    The label keeps the same file name in two directories distinct.
    """
    for name in names:
        path = directory / name
        if not path.exists():
            continue
        real = path.resolve()
        if base != real and base not in real.parents:
            raise ValueError(f"input {real} escapes the watched directory {base}")
        h.update(f"{label}/{name}".encode())
        with real.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)


def fingerprint(
    raw_dir: Path,
    geopackages: list[str],
    frame_dir: Path,
    derived_dir: Path,
    dbt_digest: str,
    population_year: int,
) -> str:
    """Fingerprint a refresh's full input set; see the module docstring.

    Hashes file CONTENTS, not size and mtime, so a same-size replacement with a
    preserved mtime is still detected. Every file must stay inside its own
    directory: a name that is a symlink to bytes elsewhere on the host raises,
    so the sensor never hashes, and a run never publishes, anything outside the
    inputs it watches.
    """
    h = hashlib.sha256()
    _hash_confined(h, raw_dir.resolve(), raw_dir, geopackages, "raw")
    _hash_confined(h, frame_dir.resolve(), frame_dir, input_files(frame_dir), FRAME_SUBDIR)
    _hash_confined(h, derived_dir.resolve(), derived_dir, input_files(derived_dir), DERIVED_SUBDIR)
    h.update(f"dbt/{dbt_digest}".encode())
    h.update(f"population_year/{population_year}".encode())
    return h.hexdigest()[:16]
