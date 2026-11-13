"""
Export a published register as an open data release.

The release is two sets of files, because the register mixes licences. The open
set (CC BY 4.0) holds every exported column derived from attribution-only
sources, and the LGA boundaries as a GeoPackage. The non-commercial set (CC
BY-NC-SA 3.0 IGO) holds the columns derived from ESPEN: the endemicity ranks
and everything built from them, which includes the priority score. Each table
appears in both sets under the same key columns, so a user who accepts the
non-commercial terms joins the two.

Every column of every exported table is classified below. The export refuses to
run when a table has a column the classification does not name, so a new
ESPEN-derived column can never reach the open set by default.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import duckdb
import geopandas as gpd
import shapely
from shapely.ops import unary_union

from nfr.orchestration import publish

OPEN_LICENCE = "CC BY 4.0"
NON_COMMERCIAL_LICENCE = "CC BY-NC-SA 3.0 IGO"
OPEN_SET = "cc-by"
NON_COMMERCIAL_SET = "non-commercial"
RELEASE_LABEL_PATTERN = re.compile(r"^v\d+\.\d+\.\d+$")
BOUNDARIES_FILE = "lga_boundaries.gpkg"
BOUNDARIES_LAYER = "lga_boundaries"
EXPECTED_LGAS = 774
# Copied into both sets, relative to the repository root, so each archive
# carries its own terms and column definitions.
BUNDLED_DOCS = ("LICENSE-DATA", "data/ATTRIBUTION.md", "docs/DATA_CONTRACT.md")

_ENDEMICITY_RANKS = ("sth_endemicity_rank_lga", "sch_endemicity_rank_lga", "lf_endemicity_rank_lga",
                     "oncho_endemicity_rank_lga", "loa_endemicity_rank_lga")


@dataclass(frozen=True)
class TableSplit:
    """How one register table divides between the two sets.

    `keys` go in both sets, so they join. `excluded` columns ship in neither:
    each is available elsewhere in the release or is dashboard presentation.
    """

    keys: tuple[str, ...]
    open: tuple[str, ...]
    non_commercial: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()

    def classified(self) -> set[str]:
        return {*self.keys, *self.open, *self.non_commercial, *self.excluded}


SPLITS: dict[str, TableSplit] = {
    "lga_coverage_burden": TableSplit(
        keys=("state", "canonical_lga", "lga_label"),
        open=(
            "facilities", "facility_source_version", "facility_status", "facilities_primary",
            "facilities_secondary", "facilities_tertiary", "facilities_level_unknown", "facilities_functional",
            "facilities_not_functional", "facilities_status_unknown", "population", "population_year", "births",
            "facilities_per_10k_population", "facilities_per_10k_births", "secondary_tertiary_per_100k_population",
            "functional_status_known_share", "not_functional_share", "motorised_minutes", "walking_minutes",
            "walking_over_60min_share", "access_population_share", "flood_affected_2022_state",
            "flood_affected_2024", "malaria_year", "pf_parasite_rate", "pf_incidence_per_1000", "hard_to_reach",
            "access_risk_score", "priority_coverage_points", "priority_malaria_points",
        ),
        non_commercial=(
            "espen_iu_id", "sth_high_share_pct_state", "sth_survey_years_state", *_ENDEMICITY_RANKS,
            "ntd_diseases_known", "priority_high_burden_thin_coverage", "priority_burden_points",
            "priority_score", "priority_tier", "priority_reasons",
        ),
        # The one-line summary quotes the ranks and the score; it is the map hover.
        excluded=("lga_summary",),
    ),
    "lga_coverage_outlook": TableSplit(
        keys=("state", "canonical_lga", "population_year"),
        open=("lga_label", "is_register_year", "is_last_projection_year", "population", "facilities",
              "facility_status", "facilities_per_10k_population", "population_change_from_register_year",
              "facilities_per_10k_change_from_register_year"),
    ),
    "lga_version_drift": TableSplit(
        keys=("facility_id",),
        open=("lineage_status", "canonical_lga", "source_lga", "state", "v2_id", "v3_id", "v2_name", "v3_name",
              "match_distance_m", "name_similarity", "name_match_band"),
        # The LGA's IU id ships once, in the non-commercial lga_coverage_burden.
        excluded=("espen_iu_id",),
    ),
    "register_summary": TableSplit(
        keys=("built_at",),
        open=("lgas", "states", "states_on_v3", "states_on_v2", "states_without_facilities", "v3_source_date",
              "v2_source_date", "facilities", "facilities_in_lgas", "facilities_v3", "facilities_v2",
              "lgas_unmatched", "population", "population_year", "facilities_secondary_tertiary",
              "facilities_level_unknown", "facilities_functional_status_known", "facilities_not_functional",
              "hard_to_reach_lgas", "flood_affected_2024_lgas", "lineage_carried_over", "lineage_dropped",
              "lineage_appeared"),
        non_commercial=("priority_lgas", "priority_high_tier_lgas", "sth_known_lgas", "sch_known_lgas",
                        "lf_known_lgas", "oncho_known_lgas", "loa_known_lgas", "espen_latest_year"),
    ),
    "lga_history": TableSplit(
        keys=("built_at", "fingerprint", "state", "canonical_lga", "lga_label"),
        open=("facilities", "facilities_per_10k_population"),
        non_commercial=("priority_score", "priority_tier"),
    ),
}

# Read for the GeoPackage only; its rows are polygon parts, not LGAs.
_BOUNDARY_SOURCE = (
    "select state, canonical_lga, lga_label, geometry_geojson from lga_coverage_choropleth "
    "order by state, canonical_lga"
)
_TABLE_COLUMNS = "select column_name from information_schema.columns where table_schema = 'main' and table_name = ?"


class OpenDataError(RuntimeError):
    """The register cannot be exported as it stands; the message says what to fix."""


@dataclass(frozen=True)
class ExportedFile:
    name: str
    rows: int
    licence: str
    sha256: str


def check_classification(con: duckdb.DuckDBPyConnection) -> None:
    """Raise OpenDataError unless every exported table's columns match its split exactly."""
    problems = []
    for table, split in SPLITS.items():
        actual = {row[0] for row in con.execute(_TABLE_COLUMNS, [table]).fetchall()}
        if not actual:
            problems.append(f"{table}: missing from the register")
            continue
        if unclassified := sorted(actual - split.classified()):
            problems.append(f"{table}: unclassified columns {', '.join(unclassified)}")
        if gone := sorted(split.classified() - actual):
            problems.append(f"{table}: classified columns not in the register {', '.join(gone)}")
    if problems:
        raise OpenDataError(
            "the register's columns do not match the licence classification in nfr/open_data.py; "
            "classify each column as open, non-commercial or excluded, then export again: " + "; ".join(problems)
        )


def export_release(db_path: Path, out_dir: Path, label: str, repo_root: Path) -> list[Path]:
    """Write both sets for `label` under `out_dir` and return the two zip archives.

    The register is opened read-only and every table is read through one
    connection, so the release describes a single published register even if a
    refresh promotes a new one meanwhile. Both sets are built in a scratch
    directory and moved into `out_dir` only once both succeed, so a failed
    export never leaves one archive without the other, or a stale pair from an
    earlier run looking current.
    """
    if not RELEASE_LABEL_PATTERN.match(label):
        raise OpenDataError(f"expected a release label such as v0.14.0, got {label!r}")
    if not db_path.is_file():
        raise OpenDataError(f"no register at {db_path}; run a refresh first")
    fingerprint = publish.read_published_fingerprint(str(db_path))
    if not fingerprint:
        raise OpenDataError(f"{db_path} has no published fingerprint; export a register that a refresh published")
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        check_classification(con)
        out_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=out_dir, prefix=".partial-") as scratch:
            built = [_build_set(con, Path(scratch), label, fingerprint, set_name, licence, repo_root)
                     for set_name, licence in ((OPEN_SET, OPEN_LICENCE),
                                               (NON_COMMERCIAL_SET, NON_COMMERCIAL_LICENCE))]
            archives = []
            for release_dir, archive in built:
                _move_into(out_dir, release_dir)
                archives.append(_move_into(out_dir, archive))
            return archives
    finally:
        con.close()


def _move_into(out_dir: Path, path: Path) -> Path:
    """Move `path` into `out_dir`, replacing an earlier export's copy."""
    target = out_dir / path.name
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink(missing_ok=True)
    return Path(shutil.move(str(path), str(target)))


def _build_set(con: duckdb.DuckDBPyConnection, scratch: Path, label: str, fingerprint: str, set_name: str,
               licence: str, repo_root: Path) -> tuple[Path, Path]:
    """Write one set's files and its zip under `scratch`; return the directory and the zip."""
    release_dir = scratch / f"nfr-open-data-{label}-{set_name}"
    release_dir.mkdir()
    files = list(_write_tables(con, release_dir, set_name, licence))
    if set_name == OPEN_SET:
        files.append(_write_boundaries(con, release_dir))
    for doc in BUNDLED_DOCS:
        shutil.copy2(repo_root / doc, release_dir / Path(doc).name)
    _write_manifest(release_dir, label, fingerprint, set_name, licence, files)
    (release_dir / "README.md").write_text(_readme(label, fingerprint, set_name, licence, files))
    archive = Path(shutil.make_archive(str(release_dir), "zip", scratch, release_dir.name))
    return release_dir, archive


def _columns_for(split: TableSplit, set_name: str) -> tuple[str, ...]:
    if set_name == OPEN_SET:
        return split.open
    if set_name == NON_COMMERCIAL_SET:
        return split.non_commercial
    raise OpenDataError(f"unknown set {set_name!r}; expected {OPEN_SET!r} or {NON_COMMERCIAL_SET!r}")


def _write_tables(con: duckdb.DuckDBPyConnection, release_dir: Path, set_name: str,
                  licence: str) -> Iterator[ExportedFile]:
    for table, split in SPLITS.items():
        columns = _columns_for(split, set_name)
        if not columns:
            continue
        # Identifiers come only from SPLITS, never from input, and are quoted.
        select = (f"select {', '.join(_quote(c) for c in (*split.keys, *columns))} from {_quote(table)} "
                  f"order by {', '.join(_quote(k) for k in split.keys)}")
        expected = con.execute(f"select count(*) from {_quote(table)}").fetchone()[0]
        for suffix, options in (("parquet", "format parquet"), ("csv", "format csv, header true")):
            path = release_dir / f"{table}.{suffix}"
            con.execute(f"copy ({select}) to '{_literal(path)}' ({options})")
            yield _verified(con, path, expected, licence)


def _write_boundaries(con: duckdb.DuckDBPyConnection, release_dir: Path) -> ExportedFile:
    """One MultiPolygon per LGA, rejoined from the choropleth's polygon parts."""
    parts: dict[tuple[str, str, str], list] = {}
    for state, lga, label, geojson in con.execute(_BOUNDARY_SOURCE).fetchall():
        parts.setdefault((state, lga, label), []).append(shapely.from_geojson(geojson))
    if len(parts) != EXPECTED_LGAS:
        raise OpenDataError(f"expected {EXPECTED_LGAS} LGA boundaries, the register has {len(parts)}")
    frame = gpd.GeoDataFrame(
        {"state": [k[0] for k in parts], "canonical_lga": [k[1] for k in parts], "lga_label": [k[2] for k in parts]},
        geometry=[shapely.MultiPolygon(shapely.get_parts(unary_union(p))) for p in parts.values()],
        crs="EPSG:4326",
    )
    path = release_dir / BOUNDARIES_FILE
    frame.to_file(path, layer=BOUNDARIES_LAYER, driver="GPKG")
    written = len(gpd.read_file(path, layer=BOUNDARIES_LAYER))
    if written != EXPECTED_LGAS:
        raise OpenDataError(f"{path.name} holds {written} LGAs after writing, expected {EXPECTED_LGAS}")
    return ExportedFile(path.name, written, OPEN_LICENCE, _sha256(path))


def _verified(con: duckdb.DuckDBPyConnection, path: Path, expected: int, licence: str) -> ExportedFile:
    reader = "read_parquet" if path.suffix == ".parquet" else "read_csv"
    written = con.execute(f"select count(*) from {reader}('{_literal(path)}')").fetchone()[0]
    if written != expected:
        raise OpenDataError(f"{path.name} holds {written} rows after writing, the register has {expected}")
    return ExportedFile(path.name, written, licence, _sha256(path))


def _write_manifest(release_dir: Path, label: str, fingerprint: str, set_name: str, licence: str,
                    files: list[ExportedFile]) -> None:
    manifest = {
        "release": label,
        "set": set_name,
        "licence": licence,
        "input_fingerprint": fingerprint,
        "files": [{"name": f.name, "rows": f.rows, "licence": f.licence, "sha256": f.sha256} for f in files],
    }
    (release_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def _readme(label: str, fingerprint: str, set_name: str, licence: str, files: list[ExportedFile]) -> str:
    if set_name == OPEN_SET:
        terms = (f"Licence: {licence}. These columns derive from attribution-only sources (GRID3, geoBoundaries, "
                 "WorldPop, the Malaria Atlas Project) and NASA open data. Credit the sources as ATTRIBUTION.md "
                 "sets out. The NTD endemicity ranks and the priority score are not here: they derive from ESPEN, "
                 "which is non-commercial, and ship in the separate non-commercial set.")
    else:
        terms = (f"Licence: {licence}. NON-COMMERCIAL and share-alike. These columns derive from ESPEN (WHO/AFRO): "
                 "the NTD endemicity ranks and everything built from them, including the priority score, tier "
                 "and reasons. Use them only on ESPEN's terms (LICENSE-DATA). Join them to the open set on the "
                 "key columns each table shares with it.")
    rows = "\n".join(f"| `{f.name}` | {f.rows} | {f.licence} |" for f in files)
    return (
        f"# Nigeria facility register: open data {label} ({set_name})\n\n"
        f"{terms}\n\n"
        f"Built from the published register with input fingerprint `{fingerprint}`. `register_summary.built_at` "
        "gives its build time. DATA_CONTRACT.md defines every table and column; a column's meaning and its "
        "missing-is-not-zero rules are the same here as in the register. `manifest.json` lists each file's row "
        "count and SHA-256.\n\n"
        "| File | Rows | Licence |\n| --- | --- | --- |\n"
        f"{rows}\n\n"
        "Each table comes as Parquet and as CSV with the same rows. This is an engineering reading of the "
        "published terms, not legal advice.\n"
    )


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _literal(path: Path) -> str:
    return str(path).replace("'", "''")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
