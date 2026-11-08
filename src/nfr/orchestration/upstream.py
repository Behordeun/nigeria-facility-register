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
- Malaria Atlas Project malaria: the P. falciparum parasite-rate and incidence
  releases its Malaria WCS advertises. That workspace keeps every older release
  listed, so only a release dated after the pinned one is reported.
- ESPEN: the portal's public download route (the one its "Download data" form
  calls, no key) is asked for Nigeria IU-level rows newer than the pinned latest
  year, once per disease. An empty answer means nothing newer; any rows mean a
  new year to review.

Nothing here downloads data or changes the register. A publisher that cannot be
reached is reported as unreachable, and one that answers in a shape the parsers
do not recognise as unreadable, rather than raising, so one failing site never
hides the others and an operator can tell a network fault from a format change.
The parsers are pure and tested without the network.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

import yaml

SOURCES = Path(__file__).resolve().parents[3] / "data" / "sources.yml"
REQUEST_TIMEOUT_S = 30
REQUEST_ATTEMPTS = 3
RETRY_BACKOFF_S = 2.0
USER_AGENT = "nigeria-facility-register upstream check"

WORLDPOP_INDEX_URL = "https://data.worldpop.org/GIS/Population/Global_2015_2030/"
HDX_SEARCH_URL = (
    "https://data.humdata.org/api/3/action/package_search?"
    + urllib.parse.urlencode({"q": "grid3 nga health facilities", "fq": "organization:grid3", "rows": 50})
)
MAP_CAPABILITIES_URL = (
    "https://data.malariaatlas.org/geoserver/Accessibility/wcs?service=WCS&version=2.0.1&request=GetCapabilities"
)
MAP_MALARIA_CAPABILITIES_URL = (
    "https://data.malariaatlas.org/geoserver/Malaria/wcs?service=WCS&version=2.0.1&request=GetCapabilities"
)

# Publisher names as they appear on findings and in the asset's metadata.
WORLDPOP = "WorldPop"
GRID3 = "GRID3 health facilities"
MAP = "Malaria Atlas Project"
MAP_MALARIA = "Malaria Atlas Project malaria"
ESPEN = "ESPEN"

ESPEN_DOWNLOAD_URL = "https://espen.afro.who.int/api/download-data/NG/{disease}/iu/{from_year}/{to_year}"
# The five diseases the register carries, by the portal's disease codes.
ESPEN_DISEASES = ("STH", "SCH", "lf", "Oncho", "Loa")

_WORLDPOP_RELEASE = re.compile(r"\bR(20\d{2})([A-Z])\b")
_GRID3_NGA_HEALTH = re.compile(r"^grid3-nga-health-facilities-v(\d+)-(\d+)$")
_MAP_HEALTHCARE = re.compile(r"Accessibility__\w*?Travel_Time_[Tt]o_Healthcare")
# A malaria coverage the register uses: release stamp (YYYYMM) and product.
_MAP_MALARIA = re.compile(r"Malaria__(\d{6})_Global_Pf_(Parasite_Rate|Incidence_Rate)\b")


class Status(str, Enum):
    CURRENT = "current"
    NEW_RELEASE = "new_release"
    UNREACHABLE = "unreachable"
    UNREADABLE = "unreadable"


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
    map_malaria_coverages: frozenset[str]


def read_pinned(sources_path: Path = SOURCES) -> Pinned:
    """The pinned WorldPop release, GRID3 versions, MAP coverages and ESPEN year, from data/sources.yml."""
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
    malaria_upstream = (sources.get("malaria_lga") or {}).get("upstream") or {}
    malaria = {
        entry["coverage_id"] for entry in malaria_upstream.values()
        if isinstance(entry, dict) and entry.get("coverage_id")
    }
    if not malaria:
        raise ValueError("data/sources.yml pins no malaria_lga.upstream coverage")
    return Pinned(release, frozenset(grid3), frozenset(coverages), espen_year, frozenset(malaria))


def _release_key(name: str) -> tuple[int, str]:
    match = _WORLDPOP_RELEASE.search(name)
    if not match:
        raise ValueError(f"not a WorldPop release name: {name}")
    return int(match[1]), match[2]


def worldpop_finding(index_html: str, pinned_release: str) -> Finding:
    """Compare the WorldPop release folders listed in index_html with the pinned release."""
    releases = {f"R{y}{s}" for y, s in _WORLDPOP_RELEASE.findall(index_html)}
    if not releases:
        return Finding(WORLDPOP, Status.UNREADABLE, "the release index listed no releases")
    newer = sorted((r for r in releases if _release_key(r) > _release_key(pinned_release)), key=_release_key)
    if newer:
        return Finding(WORLDPOP, Status.NEW_RELEASE,
                       f"{', '.join(newer)} published; the register is pinned to {pinned_release}")
    return Finding(WORLDPOP, Status.CURRENT, f"{pinned_release} is the latest release")


def grid3_finding(search_json: dict, pinned_versions: frozenset[tuple[int, int]]) -> Finding:
    """Compare the GRID3 Nigeria health-facility datasets on HDX with the pinned versions."""
    results = ((search_json or {}).get("result") or {}).get("results") or []
    listed = set()
    for package in results:
        if match := _GRID3_NGA_HEALTH.match(str(package.get("name", ""))):
            listed.add((int(match[1]), int(match[2])))
    if not listed:
        return Finding(GRID3, Status.UNREADABLE, "HDX listed no GRID3 Nigeria health-facility dataset")
    latest_pinned = max(pinned_versions)
    newer = sorted(v for v in listed if v > latest_pinned)
    if newer:
        names = ", ".join(f"v{a}.{b}" for a, b in newer)
        return Finding(GRID3, Status.NEW_RELEASE,
                       f"{names} on HDX; the register is pinned to v{latest_pinned[0]}.{latest_pinned[1]}")
    return Finding(GRID3, Status.CURRENT,
                   f"v{latest_pinned[0]}.{latest_pinned[1]} is the latest version on HDX")


def map_finding(capabilities_xml: str, pinned_coverages: frozenset[str]) -> Finding:
    """Compare MAP's advertised travel-time-to-healthcare coverages with the pinned ones."""
    listed = set(_MAP_HEALTHCARE.findall(capabilities_xml))
    if not listed:
        return Finding(MAP, Status.UNREADABLE, "the WCS listed no travel-time-to-healthcare coverage")
    unpinned = sorted(listed - pinned_coverages)
    if unpinned:
        return Finding(MAP, Status.NEW_RELEASE,
                       f"coverages not pinned: {', '.join(unpinned)}")
    return Finding(MAP, Status.CURRENT, "only the pinned travel-time coverages are published")


def map_malaria_finding(capabilities_xml: str, pinned_coverages: frozenset[str]) -> Finding:
    """Report a MAP malaria release newer than the pinned one, for the products the register uses."""
    listed = _MAP_MALARIA.findall(capabilities_xml)
    if not listed:
        return Finding(MAP_MALARIA, Status.UNREADABLE, "the WCS listed no P. falciparum parasite-rate or incidence coverage")
    pinned = [m for c in pinned_coverages if (m := _MAP_MALARIA.search(c))]
    if not pinned:
        raise ValueError("no pinned malaria coverage has a recognisable release stamp")
    pinned_release = max(m[1] for m in pinned)
    newer = sorted({stamp for stamp, _ in listed if stamp > pinned_release})
    if newer:
        return Finding(MAP_MALARIA, Status.NEW_RELEASE,
                       f"release {', '.join(newer)} published; the register is pinned to {pinned_release}")
    return Finding(MAP_MALARIA, Status.CURRENT, f"{pinned_release} is the latest malaria release")


def espen_years(csv_text: str) -> set[int]:
    """Years present in one ESPEN download (an empty body means no rows for the filters)."""
    if not csv_text.strip():
        return set()
    reader = csv.DictReader(io.StringIO(csv_text))
    if "year" not in (reader.fieldnames or []):
        raise ValueError("ESPEN download has no year column")
    years = set()
    for row in reader:
        value = (row.get("year") or "").strip()
        if not value:
            continue
        if not value.isdigit():
            # A non-numeric year means the download's format changed; raise so the
            # finding says so (unreadable) instead of dropping rows silently.
            raise ValueError(f"ESPEN year {value!r} is not a number")
        years.add(int(value))
    return years


def espen_finding(years_by_disease: dict[str, set[int]], pinned_latest_year: int) -> Finding:
    """Report ESPEN years newer than the pinned latest year, per disease."""
    newer = {d: sorted(y for y in ys if y > pinned_latest_year) for d, ys in years_by_disease.items()}
    newer = {d: ys for d, ys in newer.items() if ys}
    if newer:
        listed = "; ".join(f"{d} {', '.join(map(str, ys))}" for d, ys in newer.items())
        return Finding(ESPEN, Status.NEW_RELEASE, f"newer years published: {listed}; the register is pinned to {pinned_latest_year}")
    return Finding(ESPEN, Status.CURRENT, f"no Nigeria IU data after {pinned_latest_year}")


def http_get(url: str, attempts: int = REQUEST_ATTEMPTS, sleep: Callable[[float], None] = time.sleep) -> str:
    """GET url as text, with a timeout, an identifying user agent and retries.

    Network errors are retried with exponential backoff (2 s, then 4 s): some
    publishers' gateways refuse an occasional connection, and a single refusal
    should not read as the publisher being down. An HTTP error status is not a
    transient refusal, so it is raised at once.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:  # noqa: S310 - fixed https URLs
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError:
            raise
        except _NETWORK_ERRORS:
            if attempt == attempts:
                raise
            sleep(RETRY_BACKOFF_S * 2 ** (attempt - 1))
    raise AssertionError("unreachable: the loop returns or raises")


def _espen(pinned: Pinned, get: Callable[[str], str], this_year: int) -> Finding:
    if this_year <= pinned.espen_latest_year:
        return Finding(ESPEN, Status.CURRENT, f"no year after {pinned.espen_latest_year} can exist yet")
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
        (WORLDPOP, WORLDPOP_INDEX_URL, lambda body: worldpop_finding(body, pinned.worldpop_release)),
        (GRID3, HDX_SEARCH_URL, lambda body: grid3_finding(json.loads(body), pinned.grid3_versions)),
        (MAP, MAP_CAPABILITIES_URL, lambda body: map_finding(body, pinned.map_coverages)),
        (MAP_MALARIA, MAP_MALARIA_CAPABILITIES_URL,
         lambda body: map_malaria_finding(body, pinned.map_malaria_coverages)),
    ]
    findings = []
    for source, url, judge in checks:
        findings.append(_guarded(source, lambda url=url, judge=judge: judge(get(url))))
    findings.append(_guarded(ESPEN, lambda: _espen(pinned, get, this_year)))
    return findings


# Network and HTTP failures: the publisher could not be reached. Anything else
# raised while reading a response means it answered in an unexpected shape.
_NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError)


def _guarded(source: str, check: Callable[[], Finding]) -> Finding:
    """Run one publisher's check; turn a failure into a finding so it cannot hide the others."""
    try:
        return check()
    except _NETWORK_ERRORS as exc:
        return Finding(source, Status.UNREACHABLE, f"{type(exc).__name__}: {exc}"[:300])
    except Exception as exc:  # a parse failure: the publisher answered, but not as expected
        return Finding(source, Status.UNREADABLE, f"{type(exc).__name__}: {exc}"[:300])
