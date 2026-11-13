"""Tests for the committed Superset dashboard bundle and its import/export helper.

The bundle is the dashboard's source of truth, so these check it stays loadable
and consistent without a running Superset: every reference resolves by UUID, the
database connection stays read-only, every physical dataset column exists in the
dbt contract of the model it reads (so a renamed mart column fails here, not as
a broken chart), and every column a chart names exists on its dataset. The two
tables not built by dbt, register_changes and lga_history, are checked against the
column lists their writers publish instead.
"""
from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest
import yaml

from nfr.orchestration import changes, history

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
    # Written at publish time, not by dbt; their writers declare their columns.
    columns[changes.TABLE] = set(changes.COLUMNS)
    columns[history.TABLE] = set(history.COLUMNS)
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


def test_sql_lab_exposure_matches_the_recorded_decision():
    # docs/adr/0001-superset-sql-lab-access.md: the register is exposed to SQL Lab,
    # read-only, with access gated by the sql_lab role. Changing this setting is a
    # decision, not a tweak: update the ADR and this test in the same change.
    (database,) = _many("databases")
    assert database["expose_in_sqllab"] is True, (
        "SQL Lab exposure changed; update docs/adr/0001-superset-sql-lab-access.md and this test together"
    )
    assert database["allow_dml"] is False, "SQL Lab exposure is only acceptable while DML is disallowed"
    assert database["sqlalchemy_uri"].endswith("access_mode=read_only"), (
        "SQL Lab exposure is only acceptable while the connection is read-only"
    )


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


def test_build_zip_marks_the_import_as_an_assets_bundle(tmp_path):
    # ImportAssetsCommand overwrites charts, datasets and the connection by UUID,
    # and accepts only a bundle typed "assets"; the committed bundle stays typed
    # "Dashboard", so the type is set at zip time and nothing else changes.
    out = tmp_path / "bundle.zip"
    sd.build_zip(BUNDLE, out)
    committed = _load(BUNDLE / "metadata.yaml")
    with zipfile.ZipFile(out) as zf:
        zipped = yaml.safe_load(zf.read(f"{sd.BUNDLE_ROOT}/metadata.yaml"))
        others = {n: zf.read(n) for n in zf.namelist() if not n.endswith("/metadata.yaml")}
    assert zipped["type"] == "assets", "the import zip must declare the assets bundle type"
    assert {k: v for k, v in zipped.items() if k != "type"} == {
        k: v for k, v in committed.items() if k != "type"
    }, "only the metadata type may differ from the committed bundle"
    for name, content in others.items():
        source = BUNDLE / Path(name).relative_to(sd.BUNDLE_ROOT)
        assert content == source.read_bytes(), f"{name} must be zipped unchanged"


def test_import_runs_the_overwriting_assets_command():
    program = sd._IMPORT_PROGRAM
    assert "ImportAssetsCommand(contents).run()" in program, "import must use the overwriting assets command"
    assert "import-dashboards" not in program, "import-dashboards keeps existing charts and datasets"
    assert sd.CONTAINER_IMPORT_ZIP in program, "the program must read the zip copied into the container"
    compile(program, "<import program>", "exec")


def test_register_path_follows_the_bundle_connection():
    assert sd.register_path(BUNDLE) == "/app/register/nfr.duckdb", \
        "the preflight must check the file the connection opens, without its query string"


def _fake_run(returncode: int, stderr: str = ""):
    def run(cmd, **kwargs):
        return sd.subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)
    return run


def test_import_preflight_names_the_refresh_when_the_register_is_missing(monkeypatch):
    monkeypatch.setattr(sd.subprocess, "run", _fake_run(1))
    with pytest.raises(SystemExit, match="refresh_register"):
        sd.require_register(None, "/app/register/nfr.duckdb")


def test_import_preflight_reports_docker_errors_as_they_are(monkeypatch, capsys):
    monkeypatch.setattr(sd.subprocess, "run", _fake_run(1, "service \"superset\" is not running"))
    with pytest.raises(SystemExit, match="command failed") as excinfo:
        sd.require_register(None, "/app/register/nfr.duckdb")
    assert "refresh_register" not in str(excinfo.value), "a stopped stack is not a missing register"
    assert "not running" in capsys.readouterr().err, "the docker error must reach the user"


def test_import_preflight_passes_when_the_register_exists(monkeypatch):
    monkeypatch.setattr(sd.subprocess, "run", _fake_run(0))
    sd.require_register(None, "/app/register/nfr.duckdb")


def test_as_assets_metadata_rejects_a_non_mapping():
    with pytest.raises(SystemExit, match="not a mapping"):
        sd.as_assets_metadata("- just\n- a list\n")


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


def test_every_map_draws_a_measured_zero():
    # The deck.gl Polygon layer leaves a polygon unfilled when its metric is
    # falsy, so a measured 0 (a non-endemic rank, access risk 0) would look the
    # same as an unmeasured LGA. Each map whose lowest band starts at 0 maps 0 to
    # 0.001, which lands in that band; NULL stays NULL and stays blank.
    maps = [c for c in _many("charts") if c["viz_type"] == "deck_polygon" and c["params"].get("break_points")]
    assert maps, "expected banded map charts in the bundle"
    for chart in maps:
        params = chart["params"]
        if float(min(params["break_points"], key=float)) != 0:
            continue
        metric = params["metric"]
        needs_sql = f"map {chart['slice_name']} needs a SQL metric so a measured 0 can be mapped to 0.001"
        assert isinstance(metric, dict), needs_sql
        assert "sqlExpression" in metric, needs_sql
        expression = metric["sqlExpression"]
        leaves_zero_blank = f"map {chart['slice_name']} would leave LGAs measuring 0 blank"
        assert expression.startswith("CASE WHEN "), leaves_zero_blank
        assert " = 0 THEN 0.001 ELSE " in expression, leaves_zero_blank


def test_sources_note_credits_every_licensed_source():
    # CC BY sources require attribution wherever their figures are shown.
    (dashboard,) = _many("dashboards")
    notes = [n["meta"]["code"] for n in dashboard["position"].values()
             if isinstance(n, dict) and n.get("type") == "MARKDOWN"]
    (sources,) = [code for code in notes if code.startswith("### Sources and licences")]
    for source in ("GRID3", "geoBoundaries", "ESPEN", "WorldPop", "Malaria Atlas Project", "NASA MODIS"):
        assert source in sources, f"the Sources and licences note does not credit {source}"


def test_lga_profile_link_targets_this_dashboard_filter_and_tab():
    # The ranked list's link hard-codes three identifiers from the dashboard:
    # its slug, the LGA native filter's id, and the profile tab's id. Renaming
    # any of them would leave every link opening the wrong place, silently.
    (dashboard,) = _many("dashboards")
    (burden,) = [d for d in _many("datasets") if d["table_name"] == "lga_coverage_burden"]
    columns = {c["column_name"]: c for c in burden["columns"]}
    expression = columns["lga_profile_link"]["expression"]

    filters = {f["id"]: f for f in dashboard["metadata"]["native_filter_configuration"]}
    lga_filter = filters.get("NATIVE_FILTER-lga")
    assert lga_filter, "the link presets NATIVE_FILTER-lga, which the dashboard no longer defines"
    assert [t["column"]["name"] for t in lga_filter["targets"]] == ["lga_label"], (
        "the link sets lga_label; the LGA filter must target that column"
    )
    assert f"/superset/dashboard/{dashboard['slug']}/?native_filters=(NATIVE_FILTER-lga:" in expression, (
        "the link must open this dashboard by its slug"
    )
    assert expression.count("#TAB-profile") == 1 and "TAB-profile" in dashboard["position"], (
        "the link must open the LGA profile tab, which must exist"
    )
