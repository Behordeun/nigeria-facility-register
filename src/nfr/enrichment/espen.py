"""
ESPEN NTD endemicity per LGA, from ESPEN's IU-level exports.

ESPEN (the WHO Expanded Special Project for Elimination of NTDs) publishes, per
implementation unit (IU) and year, an endemicity status for each disease. Its
data portal serves the IU-level table for a country, disease and year range as
CSV from a public download route that needs no key. In Nigeria an IU is an LGA,
and the master LGA index carries each LGA's ESPEN IU id, so the exports map onto
the 774-LGA frame directly.

The register keeps one row per (LGA, disease): the latest year's endemicity as
published, and separately the latest label that is not an "Unknown ..." status,
so a recent unknown never hides an older measured status and is never read as a
measured low. ESPEN is CC BY-NC-SA 3.0 IGO, so only this summary is committed,
never the IU-level exports.
"""
from __future__ import annotations

import pandas as pd

DOWNLOAD_URL = "https://espen.afro.who.int/api/download-data/NG/{code}/iu/{first}/{last}"
FIRST_YEAR = 2014

# ESPEN's disease codes in the download route, and the names the register uses.
DISEASE_CODES = {
    "STH": "Soil-transmitted helminths",
    "SCH": "Schistosomiasis",
    "lf": "Lymphatic filariasis",
    "Oncho": "Onchocerciasis",
    "Loa": "Loa loa",
}
ESPEN_DISEASES = set(DISEASE_CODES.values())

ESPEN_COLUMNS = [
    "state", "canonical_lga", "espen_iu_id", "disease", "disease_code",
    "endemicity_latest", "year_latest", "endemicity_latest_known",
    "year_latest_known", "years_reported",
]


def download_url(code: str, last_year: int, first_year: int = FIRST_YEAR) -> str:
    """The portal's public download route for one disease's IU-level export."""
    if code not in DISEASE_CODES:
        raise ValueError(f"unknown ESPEN disease code {code!r}; expected one of {sorted(DISEASE_CODES)}")
    return DOWNLOAD_URL.format(code=code, first=first_year, last=last_year)


def is_unknown(label: object) -> bool:
    """True for a missing label or any ESPEN "Unknown ..." status."""
    return pd.isna(label) or str(label).strip().lower().startswith("unknown")


def long_from_exports(exports: dict[str, pd.DataFrame], master_index: pd.DataFrame) -> pd.DataFrame:
    """Stack the per-disease exports into one long table keyed to the LGA frame.

    ESPEN pads its IU-by-year grid with rows that carry no year or no
    endemicity; those are not surveys and are dropped. A surveyed "Non-endemic"
    is kept, since it is a real finding. Each IU is mapped to its LGA through
    the master index's ESPEN IU id; an IU the index does not know fails the
    build rather than being dropped.
    """
    if missing := set(DISEASE_CODES) - set(exports):
        raise SystemExit(f"espen: missing exports for {sorted(missing)}")
    frames = []
    for code, disease in DISEASE_CODES.items():
        df = exports[code]
        if needed := {"iuId", "year", "endemicity"} - set(df.columns):
            raise SystemExit(f"espen {code}: export lacks columns {sorted(needed)} (an empty or error page?)")
        sub = df.loc[df["year"].notna() & df["endemicity"].notna(), ["iuId", "year", "endemicity"]].copy()
        sub["year"] = pd.to_numeric(sub["year"]).astype("Int64")
        sub["iuId"] = sub["iuId"].astype(str)
        sub["disease"] = disease
        sub["disease_code"] = code
        frames.append(sub)
    long = pd.concat(frames, ignore_index=True)

    bridge = (master_index[["espen_iu_id", "canonical_lga", "state"]].dropna(subset=["espen_iu_id"])
              .astype(str).rename(columns={"espen_iu_id": "iuId"}).drop_duplicates("iuId"))
    long = long.merge(bridge, on="iuId", how="left")
    if unmapped := sorted(long.loc[long["canonical_lga"].isna(), "iuId"].unique()):
        raise SystemExit(f"espen: {len(unmapped)} IU ids are not in the master LGA index, e.g. {unmapped[:3]}")
    return long


def derive_espen(raw: pd.DataFrame) -> pd.DataFrame:
    """Reduce the ESPEN long table to one row per (LGA, disease)."""
    required = {"iuId", "state", "canonical_lga", "disease", "disease_code", "year", "endemicity"}
    if missing := required - set(raw.columns):
        raise SystemExit(f"espen: missing columns {sorted(missing)}")
    if unexpected := set(raw["disease"].dropna()) - ESPEN_DISEASES:
        raise SystemExit(f"espen: unexpected diseases {sorted(unexpected)}")

    keys = ["iuId", "state", "canonical_lga", "disease", "disease_code", "year"]
    per_year = raw[keys + ["endemicity"]].drop_duplicates()
    clash = per_year.groupby(["iuId", "disease", "year"])["endemicity"].nunique(dropna=False)
    if (clash > 1).any():
        sample = clash[clash > 1].index[:3].tolist()
        raise SystemExit(f"espen: conflicting endemicity for one IU-year, e.g. {sample}")
    if per_year.duplicated(["iuId", "disease", "year"]).any():
        raise SystemExit("espen: one IU-disease-year maps to more than one LGA")

    group = ["state", "canonical_lga", "iuId", "disease", "disease_code"]
    per_year = per_year.sort_values(group + ["year"])
    latest = per_year.groupby(group, as_index=False).last()
    latest = latest.rename(columns={"endemicity": "endemicity_latest", "year": "year_latest"})

    known = per_year[~per_year["endemicity"].map(is_unknown)]
    latest_known = known.groupby(group, as_index=False).last().rename(
        columns={"endemicity": "endemicity_latest_known", "year": "year_latest_known"}
    )
    years = per_year.groupby(group, as_index=False)["year"].nunique().rename(
        columns={"year": "years_reported"}
    )

    out = latest.merge(latest_known, on=group, how="left").merge(years, on=group, how="left")
    out = out.rename(columns={"iuId": "espen_iu_id"})
    # A disease never reported with a known status has no latest-known year;
    # keep it null (nullable Int64), never 0.
    out["year_latest_known"] = out["year_latest_known"].astype("Int64")
    return out[ESPEN_COLUMNS].sort_values(["state", "canonical_lga", "disease"]).reset_index(
        drop=True
    )
