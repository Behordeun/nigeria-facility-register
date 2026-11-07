"""
Import the committed Superset dashboard bundle into the compose Superset, or
export it back after editing it in the Superset UI.

The dashboard ("Nigeria facility coverage and NTD burden") lives in the repo as
a Superset import bundle under superset/dashboards/nfr_coverage_burden/: the
read-only DuckDB connection, the four datasets it reads, every chart and the
dashboard layout, all keyed by stable UUIDs. Keeping it as files makes the
dashboard reviewable in a PR and rebuildable from an empty superset_home volume.

    import  zip the bundle, copy it into the running superset service and load it
            with Superset's ImportAssetsCommand, which overwrites the database
            connection, every dataset, every chart and the dashboard by UUID. A
            re-run therefore updates all of them in place rather than duplicating
            them. `superset import-dashboards` is not used: it overwrites only
            the dashboard and keeps any chart, dataset or connection that already
            exists, so an upgraded deployment would keep stale charts.
    export  export ONLY this dashboard (by UUID) from the running Superset and
            write it back over the bundle directory. `superset export-dashboards`
            would export every dashboard on the instance, so this selects by UUID
            instead, keeping unrelated dashboards out of the repo. Filenames are
            normalised (Superset appends instance-specific numeric ids) so a
            re-export only shows real changes in the diff.

Both talk to the compose `superset` service through `docker compose`, so the
stack must be up (`docker compose up -d`). No credential passes through this
script: the import is attributed to the admin user named by
SUPERSET_ADMIN_USERNAME inside the container, which must exist (Superset skips
an overwrite it cannot attribute to a user allowed to write charts).

Usage:
    python scripts/superset_dashboard.py import [--project NAME]
    python scripts/superset_dashboard.py export [--project NAME]
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BUNDLE_DIR = ROOT / "superset" / "dashboards" / "nfr_coverage_burden"
DASHBOARD_UUID = "435de0dd-d46c-52d5-a5c1-e75ed9f32288"
SERVICE = "superset"
# Separate container paths per direction: `docker compose cp` writes the import
# zip as root, and the export runs as the superset user, which could not
# overwrite a root-owned file left at a shared path.
CONTAINER_IMPORT_ZIP = "/tmp/nfr_dashboard_import.zip"
CONTAINER_EXPORT_ZIP = "/tmp/nfr_dashboard_export.zip"
BUNDLE_ROOT = "nfr_dashboard"
# ImportAssetsCommand only accepts a bundle whose metadata declares this type;
# the committed bundle keeps the "Dashboard" type that the export writes.
ASSETS_TYPE = "assets"
# Superset suffixes chart and dashboard filenames with the instance's numeric id.
_ID_SUFFIX = re.compile(r"_\d+(\.yaml)$")

# Runs inside the superset container (fed on stdin to `python -`): load the zip
# written by build_zip with ImportAssetsCommand, which overwrites the connection,
# datasets, charts and dashboard by UUID in one transaction.
_IMPORT_PROGRAM = f"""
import os
import sys
from zipfile import ZipFile
from flask import g
from superset.app import create_app

app = create_app()
with app.app_context():
    from superset import security_manager
    from superset.commands.importers.v1.assets import ImportAssetsCommand
    from superset.commands.importers.v1.utils import get_contents_from_bundle

    username = os.environ.get("SUPERSET_ADMIN_USERNAME", "admin")
    g.user = security_manager.find_user(username=username)
    if g.user is None:
        sys.exit(f"Superset user {{username!r}} (SUPERSET_ADMIN_USERNAME) does not exist")
    with ZipFile("{CONTAINER_IMPORT_ZIP}") as bundle:
        contents = get_contents_from_bundle(bundle)
    ImportAssetsCommand(contents).run()
"""

# Runs inside the superset container (fed on stdin to `python -`): export the one
# dashboard with this UUID, with its charts, datasets and database, to a zip.
_EXPORT_PROGRAM = f"""
import sys
from zipfile import ZipFile
from flask import g
from superset.app import create_app

app = create_app()
with app.app_context():
    import os
    from superset import db, security_manager
    from superset.commands.dashboard.export import ExportDashboardsCommand
    from superset.models.dashboard import Dashboard

    g.user = security_manager.find_user(username=os.environ.get("SUPERSET_ADMIN_USERNAME", "admin"))
    dash = db.session.query(Dashboard).filter_by(uuid="{DASHBOARD_UUID}").one_or_none()
    if dash is None:
        sys.exit("dashboard {DASHBOARD_UUID} is not in this Superset; run the import first")
    with ZipFile("{CONTAINER_EXPORT_ZIP}", "w") as bundle:
        for name, content in ExportDashboardsCommand([dash.id]).run():
            with bundle.open("{BUNDLE_ROOT}/" + name, "w") as fp:
                fp.write(content().encode())
"""


def _compose(project: str | None) -> list[str]:
    cmd = ["docker", "compose", "-f", str(ROOT / "docker-compose.yml")]
    if project:
        cmd += ["-p", project]
    return cmd


def _run(cmd: list[str], stdin: str | None = None) -> None:
    result = subprocess.run(cmd, input=stdin, text=True, capture_output=True)
    if result.returncode != 0:
        # stderr first: it carries the actionable error.
        sys.stderr.write(result.stderr[-4000:] + result.stdout[-4000:])
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(cmd[:6])} ...")


def as_assets_metadata(text: str) -> str:
    """Return metadata.yaml text with its type set to the assets bundle type.

    Every other key (version, timestamp) is kept. Raises SystemExit when the
    text is not a mapping, so a malformed bundle fails before reaching Superset.
    """
    metadata = yaml.safe_load(text)
    if not isinstance(metadata, dict):
        raise SystemExit("metadata.yaml is not a mapping")
    metadata["type"] = ASSETS_TYPE
    return yaml.safe_dump(metadata, sort_keys=False)


def build_zip(bundle_dir: Path, out: Path) -> int:
    """Zip the bundle under a single root folder as an assets bundle for import.

    Every file is copied unchanged except metadata.yaml, whose type becomes
    "assets" so ImportAssetsCommand accepts it. Returns the number of YAML files
    written. Raises SystemExit when the directory is not a bundle (no
    metadata.yaml), so a wrong path fails loudly.
    """
    if not (bundle_dir / "metadata.yaml").is_file():
        raise SystemExit(f"not a Superset bundle (no metadata.yaml): {bundle_dir}")
    files = sorted(p for p in bundle_dir.rglob("*.yaml") if p.is_file())
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            rel = path.relative_to(bundle_dir).as_posix()
            if rel == "metadata.yaml":
                zf.writestr(f"{BUNDLE_ROOT}/{rel}", as_assets_metadata(path.read_text(encoding="utf-8")))
            else:
                zf.write(path, f"{BUNDLE_ROOT}/{rel}")
    return len(files)


def normalise_export(zip_path: Path, bundle_dir: Path) -> int:
    """Replace bundle_dir with the exported bundle, stripping numeric id suffixes.

    Returns the number of files written. The directory is rebuilt from scratch so
    a chart deleted in Superset also disappears from the repo.
    """
    staging = Path(tempfile.mkdtemp(prefix="nfr_bundle_"))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                parts = Path(info.filename).parts
                if len(parts) < 2 or ".." in parts:
                    raise SystemExit(f"unexpected path in export: {info.filename}")
                rel = Path(*parts[1:])
                rel = rel.with_name(_ID_SUFFIX.sub(r"\1", rel.name))
                target = staging / rel
                # Containment, not just the ".." check: whatever the entry name
                # resolves to must stay inside the staging directory.
                if not target.resolve().is_relative_to(staging.resolve()):
                    raise SystemExit(f"unexpected path in export: {info.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))
        count = sum(1 for p in staging.rglob("*.yaml"))
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir)
        bundle_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staging, bundle_dir)
        return count
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def do_import(project: str | None) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "bundle.zip"
        count = build_zip(BUNDLE_DIR, zip_path)
        _run(_compose(project) + ["cp", str(zip_path), f"{SERVICE}:{CONTAINER_IMPORT_ZIP}"])
    _run(_compose(project) + ["exec", "-T", SERVICE, "python", "-"], stdin=_IMPORT_PROGRAM)
    print(f"imported {count} bundle files into Superset ({BUNDLE_DIR.relative_to(ROOT)})")


def do_export(project: str | None) -> None:
    _run(_compose(project) + ["exec", "-T", SERVICE, "python", "-"], stdin=_EXPORT_PROGRAM)
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "export.zip"
        _run(_compose(project) + ["cp", f"{SERVICE}:{CONTAINER_EXPORT_ZIP}", str(zip_path)])
        count = normalise_export(zip_path, BUNDLE_DIR)
    print(f"exported {count} files to {BUNDLE_DIR.relative_to(ROOT)}; review the diff before committing")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=["import", "export"])
    parser.add_argument("--project", help="docker compose project name (default: the compose default)")
    args = parser.parse_args(argv)
    if args.action == "import":
        do_import(args.project)
    else:
        do_export(args.project)


if __name__ == "__main__":
    main()
