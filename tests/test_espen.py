"""ESPEN exports to the per-(LGA, disease) table.

The committed ntd_espen_lga.csv is rebuilt from ESPEN's public download route,
so the mapping from the five IU-level exports onto the LGA frame must keep real
findings, drop ESPEN's grid padding, and refuse an export or IU it cannot place.
"""
from __future__ import annotations

import pandas as pd
import pytest

from nfr.enrichment import espen

MASTER = pd.DataFrame({
    "espen_iu_id": ["101", "102"],
    "canonical_lga": ["Alpha", "Beta"],
    "state": ["Abia", "Abia"],
})


def _export(rows):
    return pd.DataFrame(rows, columns=["iuId", "year", "endemicity", "targetPop"])


def _all_exports(**overrides):
    base = {code: _export([(101, 2024, "Non-endemic", "Total"), (102, 2024, "Endemic", "Total")])
            for code in espen.DISEASE_CODES}
    return base | overrides


def test_download_url_uses_the_portal_route():
    assert espen.download_url("Loa", 2025) == "https://espen.afro.who.int/api/download-data/NG/Loa/iu/2014/2025"


def test_download_url_rejects_an_unknown_disease():
    with pytest.raises(ValueError, match="unknown ESPEN disease code"):
        espen.download_url("Trachoma", 2025)


def test_long_table_keeps_findings_and_drops_padding():
    loa = _export([
        (101, 2023, "Non-endemic", "Total"),  # a real finding, kept
        (101, None, None, None),              # grid padding, dropped
        (102, 2023, None, "Total"),           # no endemicity, dropped
    ])
    long = espen.long_from_exports(_all_exports(Loa=loa), MASTER)
    loa_rows = long[long["disease_code"] == "Loa"]
    assert loa_rows[["iuId", "endemicity"]].values.tolist() == [["101", "Non-endemic"]]
    assert set(long["canonical_lga"]) == {"Alpha", "Beta"}, "every IU maps to its LGA"


def test_target_population_rows_collapse_to_one_label():
    # STH and schistosomiasis repeat each IU-year per MDA target group with the
    # same endemicity; the table keeps one label per (LGA, disease).
    sth = _export([(101, 2024, "High", "SAC"), (101, 2024, "High", "Total"), (102, 2024, "Low", "SAC")])
    table = espen.derive_espen(espen.long_from_exports(_all_exports(STH=sth), MASTER))
    row = table[(table["canonical_lga"] == "Alpha") & (table["disease_code"] == "STH")].iloc[0]
    assert row["endemicity_latest"] == "High"
    assert row["years_reported"] == 1


def test_a_recent_unknown_does_not_hide_an_older_measured_status():
    lf = _export([(101, 2022, "Endemic", "Total"), (101, 2025, "Unknown (consider Oncho Elimination Mapping)", "Total"),
                  (102, 2025, "Non-endemic", "Total")])
    table = espen.derive_espen(espen.long_from_exports(_all_exports(lf=lf), MASTER))
    row = table[(table["canonical_lga"] == "Alpha") & (table["disease_code"] == "lf")].iloc[0]
    assert row["endemicity_latest"].startswith("Unknown")
    assert row["endemicity_latest_known"] == "Endemic"
    assert row["year_latest_known"] == 2022


def test_an_iu_missing_from_the_index_fails_the_build():
    oncho = _export([(101, 2024, "Endemic", "Total"), (999, 2024, "Endemic", "Total")])
    exports = _all_exports(Oncho=oncho)
    with pytest.raises(SystemExit, match="not in the master LGA index"):
        espen.long_from_exports(exports, MASTER)


def test_a_missing_export_fails_the_build():
    exports = _all_exports()
    exports.pop("SCH")
    with pytest.raises(SystemExit, match="missing exports"):
        espen.long_from_exports(exports, MASTER)


def test_an_error_page_instead_of_an_export_fails_the_build():
    # ESPEN answers an empty export with a one-line "No data available." file.
    page = pd.DataFrame({"No data available.": []})
    exports = _all_exports(STH=page)
    with pytest.raises(SystemExit, match="export lacks columns"):
        espen.long_from_exports(exports, MASTER)
