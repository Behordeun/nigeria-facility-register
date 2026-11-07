"""
Watch upstream sources for releases newer than the ones the register is pinned to.

The register is built only from inputs pinned in data/sources.yml, so a new
upstream release never reaches it unreviewed. This module answers the other half
of the question: has anything newer been published that someone should review?
It reads what is pinned from data/sources.yml and compares it with what each
publisher lists today:

- WorldPop: the release folders under Global_2015_2030/ (R2024A, R2025A, ...).
  A release newer than the pinned one is reported.
- GRID3 Nigeria health facilities: the dataset slugs on HDX's catalogue API
  (grid3-nga-health-facilities-v3-0, ...). A version newer than every pinned
  one is reported. Only the catalogue is queried; the geopackage downloads sit
  behind a WAF and stay manual.
- Malaria Atlas Project: the travel-time-to-healthcare coverages its WCS
  advertises. A coverage that is not pinned (a new release) is reported.
- ESPEN: the portal's public download route (the one its "Download data" form
  calls, no key) is asked for Nigeria IU-level rows newer than the pinned latest
  year, once per disease. An empty answer means nothing newer; any rows mean a
  new year to review.

Nothing here downloads data or changes the register. A publisher that cannot be
reached is reported as unreachable rather than raising, so one slow site never
hides the others. The parsers are pure and tested without the network.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

import yaml

SOURCES = Path(__file__).resolve().parents[3] / "data" / "sources.yml"
REQUEST_TIMEOUT_S = 30
USER_AGENT = "nigeria-facility-register upstream check"

WORLDPOP_INDEX_URL = "https://data.worldpop.org/GIS/Population/Global_2015_2030/"
HDX_SEARCH_URL = (
    "https://data.humdata.org/api/3/action/package_search?"
    + urllib.parse.urlencode({"q": "grid3 nga health facilities", "fq": "organization:grid3", "rows": 50})
)
MAP_CAPABILITIES_URL = (
    "https://data.malariaatlas.org/geoserver/Accessibility/wcs?service=WCS&version=2.0.1&request=GetCapabilities"
)

ESPEN_DOWNLOAD_URL = "https://espen.afro.who.int/api/download-data/NG/{disease}/iu/{from_year}/{to_year}"
# The five diseases the register carries, by the portal's disease codes.
ESPEN_DISEASES = ("STH", "SCH", "lf", "Oncho", "Loa")

_WORLDPOP_RELEASE = re.compile(r"\bR(20\d{2})([A-Z])\b")
_GRID3_NGA_HEALTH = re.compile(r"^grid3-nga-health-facilities-v(\d+)-(\d+)$")
_MAP_HEALTHCARE = re.compile(r"Accessibility__\w*?Travel_Time_[Tt]o_Healthcare")


class Status(str, Enum):
    CURRENT = "current"
    NEW_RELEASE = "new_release"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class Finding:
    """What one upstream publisher shows compared with what is pinned."""

    source: str
    status: Status
    detail: str


@dataclass(frozen=True)
class Pinned:
    """The upstream releases the register is currently pinned to."""

    worldpop_release: str
    grid3_versions: frozenset[tuple[int, int]]
    map_coverages: frozenset[str]
    espen_latest_year: int


def read_pinned(sources_path: Path = SOURCES) -> Pinned:
    """The pinned WorldPop release, GRID3 versions and MAP coverages, from data/sources.yml."""
    sources = (yaml.safe_load(sources_path.read_text()) or {}).get("sources", {})
    upstream = (sources.get("access_population_lga") or {}).get("upstream") or {}
    release = (upstream.get("worldpop") or {}).get("release")
    if not release:
        raise ValueError("data/sources.yml has no access_population_lga.upstream.worldpop.release")
    grid3 = set()
    for entry in sources.values():
        slug = str((entry or {}).get("url", "")).rstrip("/").rsplit("/", 1)[-1]
        if match := _GRID3_NGA_HEALTH.match(slug):
            grid3.add((int(match[1]), int(match[2])))
    if not grid3:
        raise ValueError("data/sources.yml pins no GRID3 Nigeria health-facility dataset")
    coverages = {
        entry["coverage_id"] for entry in upstream.values() if isinstance(entry, dict) and entry.get("coverage_id")
    }
    espen_year = (sources.get("ntd_espen_lga") or {}).get("latest_year")
    if not isinstance(espen_year, int):
        raise ValueError("data/sources.yml has no integer ntd_espen_lga.latest_year")
    return Pinned(release, frozenset(grid3), frozenset(coverages), espen_year)


def _release_key(name: str) -> tuple[int, str]:
    match = _WORLDPOP_RELEASE.search(name)
    if not match:
        raise ValueError(f"not a WorldPop release name: {name}")
    return int(match[1]), match[2]


def worldpop_finding(index_html: str, pinned_release: str) -> Finding:
    """Compare the WorldPop release folders listed in index_html with the pinned release."""
    releases = {f"R{y}{s}" for y, s in _WORLDPOP_RELEASE.findall(index_html)}
    if not releases:
        return Finding("WorldPop", Status.UNREACHABLE, "the release index listed no releases")
    newer = sorted((r for r in releases if _release_key(r) > _release_key(pinned_release)), key=_release_key)
    if newer:
        return Finding("WorldPop", Status.NEW_RELEASE,
                       f"{', '.join(newer)} published; the register is pinned to {pinned_release}")
    return Finding("WorldPop", Status.CURRENT, f"{pinned_release} is the latest release")


def grid3_finding(search_json: dict, pinned_versions: frozenset[tuple[int, int]]) -> Finding:
    """Compare the GRID3 Nigeria health-facility datasets on HDX with the pinned versions."""
    results = ((search_json or {}).get("result") or {}).get("results") or []
    listed = set()
    for package in results:
        if match := _GRID3_NGA_HEALTH.match(str(package.get("name", ""))):
            listed.add((int(match[1]), int(match[2])))
    if not listed:
        return Finding("GRID3 health facilities", Status.UNREACHABLE, "HDX listed no GRID3 Nigeria health-facility dataset")
    latest_pinned = max(pinned_versions)
    newer = sorted(v for v in listed if v > latest_pinned)
    if newer:
        names = ", ".join(f"v{a}.{b}" for a, b in newer)
        return Finding("GRID3 health facilities", Status.NEW_RELEASE,
                       f"{names} on HDX; the register is pinned to v{latest_pinned[0]}.{latest_pinned[1]}")
    return Finding("GRID3 health facilities", Status.CURRENT,
                   f"v{latest_pinned[0]}.{latest_pinned[1]} is the latest version on HDX")


def map_finding(capabilities_xml: str, pinned_coverages: frozenset[str]) -> Finding:
    """Compare MAP's advertised travel-time-to-healthcare coverages with the pinned ones."""
    listed = set(_MAP_HEALTHCARE.findall(capabilities_xml))
    if not listed:
        return Finding("Malaria Atlas Project", Status.UNREACHABLE, "the WCS listed no travel-time-to-healthcare coverage")
    unpinned = sorted(listed - pinned_coverages)
    if unpinned:
        return Finding("Malaria Atlas Project", Status.NEW_RELEASE,
                       f"coverages not pinned: {', '.join(unpinned)}")
    return Finding("Malaria Atlas Project", Status.CURRENT, "only the pinned travel-time coverages are published")


def espen_years(csv_text: str) -> set[int]:
    """Years present in one ESPEN download (an empty body means no rows for the filters)."""
    if not csv_text.strip():
        return set()
    reader = csv.DictReader(io.StringIO(csv_text))
    if "year" not in (reader.fieldnames or []):
        raise ValueError("ESPEN download has no year column")
    return {int(row["year"]) for row in reader if row.get("year")}


def espen_finding(years_by_disease: dict[str, set[int]], pinned_latest_year: int) -> Finding:
    """Report ESPEN years newer than the pinned latest year, per disease."""
    newer = {d: sorted(y for y in ys if y > pinned_latest_year) for d, ys in years_by_disease.items()}
    newer = {d: ys for d, ys in newer.items() if ys}
    if newer:
        listed = "; ".join(f"{d} {', '.join(map(str, ys))}" for d, ys in newer.items())
        return Finding("ESPEN", Status.NEW_RELEASE, f"newer years published: {listed}; the register is pinned to {pinned_latest_year}")
    return Finding("ESPEN", Status.CURRENT, f"no Nigeria IU data after {pinned_latest_year}")


def http_get(url: str) -> str:
    """GET url as text, with a timeout and an identifying user agent."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:  # noqa: S310 - fixed https URLs
        return response.read().decode("utf-8", errors="replace")


def _espen(pinned: Pinned, get: Callable[[str], str], this_year: int) -> Finding:
    if this_year <= pinned.espen_latest_year:
        return Finding("ESPEN", Status.CURRENT, f"no year after {pinned.espen_latest_year} can exist yet")
    years = {
        disease: espen_years(get(ESPEN_DOWNLOAD_URL.format(
            disease=disease, from_year=pinned.espen_latest_year + 1, to_year=this_year)))
        for disease in ESPEN_DISEASES
    }
    return espen_finding(years, pinned.espen_latest_year)


def check_upstream(
    pinned: Pinned | None = None, get: Callable[[str], str] = http_get, today: dt.date | None = None
) -> list[Finding]:
    """One finding per publisher; never raises for a network or parse failure."""
    pinned = pinned or read_pinned()
    this_year = (today or dt.date.today()).year
    checks: list[tuple[str, str, Callable[[str], Finding]]] = [
        ("WorldPop", WORLDPOP_INDEX_URL, lambda body: worldpop_finding(body, pinned.worldpop_release)),
        ("GRID3 health facilities", HDX_SEARCH_URL, lambda body: grid3_finding(json.loads(body), pinned.grid3_versions)),
        ("Malaria Atlas Project", MAP_CAPABILITIES_URL, lambda body: map_finding(body, pinned.map_coverages)),
    ]
    findings = []
    for source, url, judge in checks:
        try:
            findings.append(judge(get(url)))
        except Exception as exc:  # one failing publisher must not hide the others
            findings.append(Finding(source, Status.UNREACHABLE, f"{type(exc).__name__}: {exc}"[:300]))
    try:
        findings.append(_espen(pinned, get, this_year))
    except Exception as exc:
        findings.append(Finding("ESPEN", Status.UNREACHABLE, f"{type(exc).__name__}: {exc}"[:300]))
    return findings
