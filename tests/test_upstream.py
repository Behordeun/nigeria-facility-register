"""Tests for nfr.orchestration.upstream: the monthly upstream-release watch.

The parsers are fed small copies of what each publisher returns, so the tests run
without the network. check_upstream is driven through an injected fetcher.
"""
import json
from datetime import date
from pathlib import Path

import pytest

from nfr.orchestration import upstream as u

PINNED = u.Pinned(
    worldpop_release="R2025A",
    grid3_versions=frozenset({(2, 0), (3, 0)}),
    map_coverages=frozenset({
        "Accessibility__202001_Global_Motorized_Travel_Time_to_Healthcare",
        "Accessibility__202001_Global_Walking_Only_Travel_Time_To_Healthcare",
    }),
    espen_latest_year=2025,
)
TODAY = date(2026, 11, 7)
ESPEN_HEADER = "id,admin0Iso2,iuId,year,endemicity\n"

WORLDPOP_INDEX = '<a href="R2024A/">R2024A/</a> <a href="R2024B/">R2024B/</a> <a href="R2025A/">R2025A/</a>'


def _hdx(*names: str) -> dict:
    return {"result": {"results": [{"name": n} for n in names]}}


def _capabilities(*ids: str) -> str:
    return "".join(f"<wcs:CoverageId>{i}</wcs:CoverageId>" for i in ids)


def test_worldpop_is_current_when_the_pinned_release_is_the_latest():
    assert u.worldpop_finding(WORLDPOP_INDEX, "R2025A").status is u.Status.CURRENT


def test_worldpop_reports_a_newer_release_in_order():
    finding = u.worldpop_finding(WORLDPOP_INDEX + '<a href="R2026A/">R2026A/</a> <a href="R2025B/">', "R2025A")
    assert finding.status is u.Status.NEW_RELEASE
    assert finding.detail.startswith("R2025B, R2026A"), "newer releases are listed oldest first"


def test_worldpop_index_without_releases_is_unreachable_not_current():
    assert u.worldpop_finding("<html>maintenance</html>", "R2025A").status is u.Status.UNREACHABLE


def test_grid3_reports_a_new_nigeria_version_and_ignores_other_countries():
    finding = u.grid3_finding(
        _hdx("grid3-nga-health-facilities-v3-0", "grid3-nga-health-facilities-v4-0", "grid3-cod-health-facilities-v9-0"),
        PINNED.grid3_versions,
    )
    assert finding.status is u.Status.NEW_RELEASE
    assert "v4.0" in finding.detail and "v9.0" not in finding.detail, "only Nigeria health-facility datasets count"


def test_grid3_is_current_when_hdx_lists_only_pinned_versions():
    finding = u.grid3_finding(_hdx("grid3-nga-health-facilities-v2-0", "grid3-nga-health-facilities-v3-0"), PINNED.grid3_versions)
    assert finding.status is u.Status.CURRENT


def test_grid3_empty_search_is_unreachable():
    assert u.grid3_finding({"result": {"results": []}}, PINNED.grid3_versions).status is u.Status.UNREACHABLE


def test_map_reports_an_unpinned_travel_time_coverage():
    new = "Accessibility__202501_Global_Walking_Only_Travel_Time_To_Healthcare"
    finding = u.map_finding(_capabilities(*PINNED.map_coverages, new, "Accessibility__201501_Global_Travel_Time_to_Cities"),
                            PINNED.map_coverages)
    assert finding.status is u.Status.NEW_RELEASE
    assert new in finding.detail and "Cities" not in finding.detail, "only healthcare travel-time coverages count"


def test_map_is_current_with_only_the_pinned_coverages():
    assert u.map_finding(_capabilities(*PINNED.map_coverages), PINNED.map_coverages).status is u.Status.CURRENT


def _pages(espen_body: str = "") -> dict:
    pages = {
        u.WORLDPOP_INDEX_URL: WORLDPOP_INDEX,
        u.HDX_SEARCH_URL: json.dumps(_hdx("grid3-nga-health-facilities-v3-0")),
        u.MAP_CAPABILITIES_URL: _capabilities(*PINNED.map_coverages),
    }
    for disease in u.ESPEN_DISEASES:
        pages[u.ESPEN_DOWNLOAD_URL.format(disease=disease, from_year=2026, to_year=2026)] = espen_body
    return pages


def test_check_upstream_reports_every_publisher_current():
    findings = u.check_upstream(PINNED, get=_pages().__getitem__, today=TODAY)
    assert [f.source for f in findings] == ["WorldPop", "GRID3 health facilities", "Malaria Atlas Project", "ESPEN"]
    assert [f.status for f in findings] == [u.Status.CURRENT] * 4


def test_check_upstream_asks_espen_only_for_years_after_the_pinned_one():
    asked = []

    def get(url):
        asked.append(url)
        return _pages()[url]

    u.check_upstream(PINNED, get=get, today=TODAY)
    espen = [a for a in asked if "espen" in a]
    assert len(espen) == len(u.ESPEN_DISEASES)
    assert all(a.endswith("/iu/2026/2026") for a in espen), "only years after 2025, up to this year"


def test_check_upstream_reports_a_new_espen_year():
    body = ESPEN_HEADER + "1,NG,36337,2026,Moderate prevalence\n"
    finding = u.check_upstream(PINNED, get=_pages(body).__getitem__, today=TODAY)[-1]
    assert finding.status is u.Status.NEW_RELEASE and "2026" in finding.detail


def test_espen_needs_no_request_when_no_later_year_can_exist():
    def get(url):
        if "espen" in url:
            raise AssertionError("no ESPEN request while the pinned year is the current year")
        return _pages()[url]

    finding = u.check_upstream(PINNED, get=get, today=date(2025, 6, 1))[-1]
    assert finding.status is u.Status.CURRENT


def test_espen_years_reads_an_empty_body_as_no_rows():
    assert u.espen_years("") == set()
    assert u.espen_years(ESPEN_HEADER + "1,NG,1,2026,x\n2,NG,2,2027,y\n") == {2026, 2027}


def test_espen_years_rejects_a_body_without_a_year_column():
    with pytest.raises(ValueError, match="no year column"):
        u.espen_years("id,endemicity\n1,x\n")


def test_check_upstream_keeps_going_when_one_publisher_fails():
    def get(url: str) -> str:
        if url == u.HDX_SEARCH_URL:
            raise OSError("timed out")
        return _pages()[url]

    findings = {f.source: f for f in u.check_upstream(PINNED, get=get, today=TODAY)}
    assert findings["GRID3 health facilities"].status is u.Status.UNREACHABLE
    assert "timed out" in findings["GRID3 health facilities"].detail
    assert findings["WorldPop"].status is u.Status.CURRENT, "a failing publisher must not hide the others"


def test_read_pinned_matches_the_committed_sources_file():
    pinned = u.read_pinned()
    assert pinned.worldpop_release == "R2025A"
    assert pinned.grid3_versions == frozenset({(2, 0), (3, 0)})
    assert len(pinned.map_coverages) == 2
    assert pinned.espen_latest_year == 2025


def test_read_pinned_refuses_a_sources_file_without_a_worldpop_release(tmp_path: Path):
    path = tmp_path / "sources.yml"
    path.write_text("sources:\n  access_population_lga:\n    upstream:\n      worldpop: {}\n")
    with pytest.raises(ValueError, match="worldpop.release"):
        u.read_pinned(path)
