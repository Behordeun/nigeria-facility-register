"""
The open data export must split every register column by licence, refuse a
register whose columns it has not classified, keep every ESPEN-derived column
out of the CC BY set, and write files whose row counts match the register.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import duckdb
import geopandas as gpd
import pandas as pd
import pytest
import yaml

from nfr import open_data
from nfr.orchestration import history, publish

ROOT = Path(__file__).resolve().parents[1]
LGAS = [("Kano", "Ajingi"), ("Rivers", "Bonny")]
# Bonny has two polygon parts, as the island LGAs do in the choropleth.
SQUARE = '{{"type":"Polygon","coordinates":[[[{x},0],[{x},1],[{x1},1],[{x1},0],[{x},0]]]}}'
ESPEN_MARKERS = ("endemicity", "espen", "priority_score", "priority_tier", "priority_reasons", "burden", "sth_",
                 "ntd_", "_known_lgas", "priority_lgas", "priority_high")


def _mart_columns() -> dict[str, list[str]]:
    schema = yaml.safe_load((ROOT / "dbt" / "models" / "marts" / "schema.yml").read_text())
    return {m["name"]: [c["name"] for c in m["columns"]] for m in schema["models"]}


def test_classification_names_exactly_the_published_columns():
    published = {**_mart_columns(), history.TABLE: list(history.COLUMNS)}
    for table, split in open_data.SPLITS.items():
        assert split.classified() == set(published[table]), (
            f"{table}: the licence split must name every published column once the schema changes"
        )
        named = [*split.keys, *split.open, *split.non_commercial, *split.excluded]
        assert len(named) == len(set(named)), f"{table}: a column is classified twice"


def test_no_espen_derived_column_is_classified_open():
    for table, split in open_data.SPLITS.items():
        leaked = [c for c in split.open if any(m in c for m in ESPEN_MARKERS)]
        assert not leaked, f"{table}: ESPEN-derived columns in the CC BY set: {leaked}"


def _register(path: Path, extra_column: str | None = None) -> Path:
    """A published register holding every exported table, with every classified column."""
    con = duckdb.connect(str(path))
    for table, split in open_data.SPLITS.items():
        columns = sorted(split.classified() | ({extra_column} if extra_column and table == "lga_coverage_burden"
                                               else set()))
        con.execute(f"create table {table} ({', '.join(f'{c} varchar' for c in columns)})")
        rows = 1 if table == "register_summary" else len(LGAS)
        for i in range(rows):
            state, lga = LGAS[i]
            values = [{"state": state, "canonical_lga": lga, "lga_label": f"{lga}, {state}",
                       "facility_id": f"f{i}"}.get(c, f"{c}-{i}") for c in columns]
            con.execute(f"insert into {table} values ({', '.join('?' for _ in columns)})", values)
    con.execute("create table lga_coverage_choropleth (state varchar, canonical_lga varchar, lga_label varchar, "
                "geometry_geojson varchar)")
    con.executemany("insert into lga_coverage_choropleth values (?, ?, ?, ?)", [
        ("Kano", "Ajingi", "Ajingi, Kano", SQUARE.format(x=0, x1=1)),
        ("Rivers", "Bonny", "Bonny, Rivers", SQUARE.format(x=5, x1=6)),
        ("Rivers", "Bonny", "Bonny, Rivers", SQUARE.format(x=8, x1=9)),
    ])
    con.close()
    publish.write_published_fingerprint(str(path), "abc123")
    return path


@pytest.fixture
def two_lgas(monkeypatch):
    monkeypatch.setattr(open_data, "EXPECTED_LGAS", len(LGAS))


def _parquet_columns(path: Path) -> list[str]:
    return duckdb.sql(f"select * from read_parquet('{path}')").columns


def _parquet_rows(path: Path) -> int:
    return duckdb.sql(f"select count(*) from read_parquet('{path}')").fetchone()[0]


def _set_dir(out: Path, set_name: str) -> Path:
    return out / f"nfr-open-data-v0.14.0-{set_name}"


def test_export_splits_the_licences_and_matches_the_register(tmp_path, two_lgas):
    out = tmp_path / "out"
    archives = open_data.export_release(_register(tmp_path / "r.duckdb"), out, "v0.14.0", ROOT)

    assert [a.name for a in archives] == ["nfr-open-data-v0.14.0-cc-by.zip",
                                          "nfr-open-data-v0.14.0-non-commercial.zip"]
    for table, split in open_data.SPLITS.items():
        rows = 1 if table == "register_summary" else len(LGAS)
        opened = _set_dir(out, "cc-by") / f"{table}.parquet"
        assert _parquet_columns(opened) == [*split.keys, *split.open], f"{table}: CC BY set is keys plus open columns"
        assert _parquet_rows(opened) == rows, f"{table}: every register row is exported"
        assert len(pd.read_csv(_set_dir(out, "cc-by") / f"{table}.csv")) == rows, f"{table}: CSV matches Parquet"
        nc_path = _set_dir(out, "non-commercial") / f"{table}.parquet"
        if split.non_commercial:
            assert _parquet_columns(nc_path) == [*split.keys, *split.non_commercial], f"{table}: NC set is keys plus ESPEN columns"
        else:
            assert not nc_path.exists(), f"{table}: a table with no ESPEN columns has no non-commercial file"
    assert not any("lga_summary" in _parquet_columns(p)
                   for p in out.rglob("*.parquet")), "the excluded summary text ships in neither set"


def test_export_writes_one_multipolygon_per_lga(tmp_path, two_lgas):
    out = tmp_path / "out"
    open_data.export_release(_register(tmp_path / "r.duckdb"), out, "v0.14.0", ROOT)
    boundaries = gpd.read_file(_set_dir(out, "cc-by") / open_data.BOUNDARIES_FILE, layer=open_data.BOUNDARIES_LAYER)
    assert len(boundaries) == len(LGAS), "polygon parts are rejoined into one row per LGA"
    assert boundaries.crs.to_epsg() == 4326
    bonny = boundaries[boundaries.canonical_lga == "Bonny"].geometry.iloc[0]
    assert len(bonny.geoms) == 2, "both of Bonny's parts are kept"


def test_each_archive_carries_its_licence_terms_and_manifest(tmp_path, two_lgas):
    out = tmp_path / "out"
    archives = open_data.export_release(_register(tmp_path / "r.duckdb"), out, "v0.14.0", ROOT)
    for archive, licence in zip(archives, (open_data.OPEN_LICENCE, open_data.NON_COMMERCIAL_LICENCE), strict=True):
        with zipfile.ZipFile(archive) as z:
            names = {Path(n).name for n in z.namelist()}
            assert {"README.md", "manifest.json", "LICENSE-DATA", "ATTRIBUTION.md", "DATA_CONTRACT.md"} <= names
            manifest = json.loads(z.read(f"{archive.stem}/manifest.json"))
            readme = z.read(f"{archive.stem}/README.md").decode()
        assert manifest["licence"] == licence and licence in readme
        assert manifest["input_fingerprint"] == "abc123"
        assert all(f["licence"] == licence for f in manifest["files"]), "every file carries its set's licence"
        assert {f["name"] for f in manifest["files"]} <= names, "the manifest lists only files in the archive"


def test_export_refuses_an_unclassified_column(tmp_path, two_lgas):
    db = _register(tmp_path / "r.duckdb", extra_column="new_espen_rank")
    with pytest.raises(open_data.OpenDataError, match="unclassified columns new_espen_rank"):
        open_data.export_release(db, tmp_path / "out", "v0.14.0", ROOT)
    assert not (tmp_path / "out").exists(), "nothing is written when a column is unclassified"


def test_a_failed_export_keeps_the_previous_archives_and_leaves_no_partial_files(tmp_path, two_lgas, monkeypatch):
    db, out = _register(tmp_path / "r.duckdb"), tmp_path / "out"
    first = open_data.export_release(db, out, "v0.14.0", ROOT)
    before = {p.name: p.read_bytes() for p in first}

    write_manifest = open_data._write_manifest

    def fail_in_second_set(release_dir, *args):
        # The open set is built first, so this fails once its archive is written.
        if release_dir.name.endswith(open_data.NON_COMMERCIAL_SET):
            raise OSError("disk full")
        write_manifest(release_dir, *args)

    monkeypatch.setattr(open_data, "_write_manifest", fail_in_second_set)
    with pytest.raises(OSError, match="disk full"):
        open_data.export_release(db, out, "v0.14.0", ROOT)

    assert {p.name: p.read_bytes() for p in first} == before, "the earlier pair of archives is left untouched"
    assert not list(out.glob(".partial-*")), "the scratch directory is removed after a failure"


def test_export_refuses_a_label_that_is_not_a_release_tag(tmp_path, two_lgas):
    with pytest.raises(open_data.OpenDataError, match="release label"):
        open_data.export_release(_register(tmp_path / "r.duckdb"), tmp_path / "out", "../v1", ROOT)


def test_export_refuses_an_unpublished_register(tmp_path, two_lgas):
    db = _register(tmp_path / "r.duckdb")
    con = duckdb.connect(str(db))
    con.execute("drop table nfr_published_vintage")
    con.close()
    with pytest.raises(open_data.OpenDataError, match="no published fingerprint"):
        open_data.export_release(db, tmp_path / "out", "v0.14.0", ROOT)
