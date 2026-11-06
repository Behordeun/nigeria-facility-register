"""Tests for the committed Superset dashboard bundle and its import/export helper.

The bundle is the dashboard's source of truth, so these check it stays loadable
and consistent without a running Superset: every reference resolves by UUID, the
database connection stays read-only, every physical dataset column exists in the
dbt contract of the model it reads (so a renamed mart column fails here, not as
a broken chart), and every column a chart names exists on its dataset.
"""
from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "superset_dashboard", ROOT / "scripts" / "superset_dashboard.py"
)
sd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd)

BUNDLE = sd.BUNDLE_DIR
CONTRACT_FILES = [ROOT / "dbt" / "models" / "marts" / "schema.yml",
                  ROOT / "dbt" / "models" / "staging" / "schema.yml"]
# Chart params that name dataset columns directly.
_COLUMN_LIST_PARAMS = ("all_columns", "groupby")
_COLUMN_SCALAR_PARAMS = ("x_axis", "line_column")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _many(subdir: str) -> list[dict]:
    return [_load(p) for p in sorted((BUNDLE / subdir).rglob("*.yaml"))]


def _contract_columns() -> dict[str, set[str]]:
    columns: dict[str, set[str]] = {}
    for path in CONTRACT_FILES:
        for model in _load(path)["models"]:
            columns[model["name"]] = {c["name"] for c in model.get("columns", [])}
    return columns


def _dataset_columns(dataset: dict) -> set[str]:
    return {c["column_name"] for c in dataset["columns"]}


def test_bundle_is_a_dashboard_export():
    meta = _load(BUNDLE / "metadata.yaml")
    assert meta["type"] == "Dashboard", "metadata.yaml must describe a dashboard bundle"
    assert len(_many("dashboards")) == 1, "expected exactly one dashboard in the bundle"
    assert _many("dashboards")[0]["uuid"] == sd.DASHBOARD_UUID, "helper UUID must match the bundled dashboard"


def test_database_connection_is_read_only_and_credential_free():
    (database,) = _many("databases")
    uri = database["sqlalchemy_uri"]
    assert uri.startswith("duckdb:////app/register/"), f"unexpected connection target: {uri}"
    assert uri.endswith("access_mode=read_only"), "the register must be opened read-only"
    assert database["allow_dml"] is False, "dashboards must not be able to write the register"
    assert database["allow_file_upload"] is False, "file upload must stay off"
    assert "password" not in database, "a bundle must never carry a credential"


def test_every_reference_resolves_by_uuid():
    (database,) = _many("databases")
    datasets = {d["uuid"]: d for d in _many("datasets")}
    charts = {c["uuid"]: c for c in _many("charts")}
    (dashboard,) = _many("dashboards")

    for ds in datasets.values():
        assert ds["database_uuid"] == database["uuid"], f"dataset {ds['table_name']} points at another database"
    for chart in charts.values():
        assert chart["dataset_uuid"] in datasets, f"chart {chart['slice_name']} references a missing dataset"

    placed = {
        node["meta"]["uuid"] for node in dashboard["position"].values()
        if isinstance(node, dict) and node.get("type") == "CHART"
    }
    assert placed == set(charts), "every chart file must be on the dashboard and every placed chart must exist"

    for flt in dashboard["metadata"]["native_filter_configuration"]:
        for target in flt["targets"]:
            assert target["datasetUuid"] in datasets, f"filter {flt['name']} targets a missing dataset"


def test_physical_dataset_columns_exist_in_the_dbt_contract():
    contract = _contract_columns()
    for ds in _many("datasets"):
        model = ds["table_name"]
        assert model in contract, f"dataset {model} reads a model with no dbt contract"
        physical = {c["column_name"] for c in ds["columns"] if not c.get("expression")}
        missing = physical - contract[model]
        assert not missing, f"dataset {model} names columns absent from its dbt contract: {sorted(missing)}"


def test_chart_columns_exist_on_their_dataset():
    datasets = {d["uuid"]: d for d in _many("datasets")}
    for chart in _many("charts"):
        available = _dataset_columns(datasets[chart["dataset_uuid"]])
        params = chart["params"]
        named = [c for key in _COLUMN_LIST_PARAMS for c in params.get(key) or []]
        named += [params[key] for key in _COLUMN_SCALAR_PARAMS if params.get(key)]
        missing = {c for c in named if isinstance(c, str)} - available
        assert not missing, f"chart {chart['slice_name']} names unknown columns: {sorted(missing)}"


def test_build_zip_nests_the_bundle_under_one_root(tmp_path):
    out = tmp_path / "bundle.zip"
    count = sd.build_zip(BUNDLE, out)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert count == len(names) > 0
    assert f"{sd.BUNDLE_ROOT}/metadata.yaml" in names, "Superset expects <root>/metadata.yaml"
    assert all(n.startswith(f"{sd.BUNDLE_ROOT}/") for n in names)


def test_build_zip_rejects_a_directory_without_metadata(tmp_path):
    with pytest.raises(SystemExit, match="not a Superset bundle"):
        sd.build_zip(tmp_path, tmp_path / "x.zip")


def _export_zip(tmp_path: Path, entries: dict[str, str]) -> Path:
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in entries.items():
            zf.writestr(name, text)
    return path


def test_normalise_export_strips_instance_ids_and_replaces_the_bundle(tmp_path):
    target = tmp_path / "bundle"
    (target / "charts").mkdir(parents=True)
    (target / "charts" / "Removed_chart.yaml").write_text("stale")
    export = _export_zip(tmp_path, {
        "root/metadata.yaml": "type: Dashboard",
        "root/charts/Facilities_per_LGA_5.yaml": "a",
        "root/dashboards/Main_12.yaml": "b",
        "root/datasets/NFR_register/lga_coverage_burden.yaml": "c",
    })
    count = sd.normalise_export(export, target)
    written = sorted(p.relative_to(target).as_posix() for p in target.rglob("*.yaml"))
    assert count == 4
    assert written == [
        "charts/Facilities_per_LGA.yaml",
        "dashboards/Main.yaml",
        "datasets/NFR_register/lga_coverage_burden.yaml",
        "metadata.yaml",
    ], "ids are stripped and a chart removed upstream disappears"


def test_normalise_export_rejects_path_traversal(tmp_path):
    export = _export_zip(tmp_path, {"root/../escape.yaml": "x"})
    with pytest.raises(SystemExit, match="unexpected path"):
        sd.normalise_export(export, tmp_path / "bundle")


def test_every_map_shows_the_lga_hover_summary():
    # The deck.gl Polygon tooltip renders only when the chart has a metric, and
    # prints the row's `name` field, so each map needs both: a metric and the
    # `name` hover column passed through js_columns.
    datasets = {d["uuid"]: d for d in _many("datasets")}
    maps = [c for c in _many("charts") if c["viz_type"] == "deck_polygon"]
    assert maps, "expected map charts in the bundle"
    for chart in maps:
        params = chart["params"]
        assert params.get("metric"), f"map {chart['slice_name']} has no metric, so it shows no tooltip"
        assert params.get("js_columns") == ["name"], f"map {chart['slice_name']} does not pass the hover summary"
        columns = {c["column_name"]: c for c in datasets[chart["dataset_uuid"]]["columns"]}
        assert columns.get("name", {}).get("expression"), "the map dataset must define the `name` hover column"
