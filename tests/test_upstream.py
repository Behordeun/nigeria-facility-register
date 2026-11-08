"""Tests for nfr.orchestration.upstream: the monthly upstream-release watch.

The parsers are fed small copies of what each publisher returns, so the tests run
without the network. check_upstream is driven through an injected fetcher.
"""
import json
import urllib.error
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
    map_malaria_coverages=frozenset({
        "Malaria__202608_Global_Pf_Parasite_Rate",
        "Malaria__202608_Global_Pf_Incidence_Rate",
    }),
)
# The malaria workspace keeps older releases listed beside the current one.
MALARIA_LISTED = (
    "Malaria__202406_Global_Pf_Parasite_Rate", "Malaria__202406_Global_Pf_Incidence_Rate",
    "Malaria__202608_Global_Pf_Parasite_Rate", "Malaria__202608_Global_Pf_Incidence_Rate",
    "Malaria__202608_Global_Pv_Parasite_Rate", "Malaria__202202_Global_Pf_Reproductive_Number",
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


def test_worldpop_index_without_releases_is_unreadable_not_current():
    assert u.worldpop_finding("<html>maintenance</html>", "R2025A").status is u.Status.UNREADABLE


def test_grid3_reports_a_new_nigeria_version_and_ignores_other_countries():
    finding = u.grid3_finding(
        _hdx("grid3-nga-health-facilities-v3-0", "grid3-nga-health-facilities-v4-0", "grid3-cod-health-facilities-v9-0"),
        PINNED.grid3_versions,
    )
    assert finding.status is u.Status.NEW_RELEASE
    assert "v4.0" in finding.detail
    assert "v9.0" not in finding.detail, "only Nigeria health-facility datasets count"


def test_grid3_is_current_when_hdx_lists_only_pinned_versions():
    finding = u.grid3_finding(_hdx("grid3-nga-health-facilities-v2-0", "grid3-nga-health-facilities-v3-0"), PINNED.grid3_versions)
    assert finding.status is u.Status.CURRENT


def test_grid3_empty_search_is_unreadable():
    assert u.grid3_finding({"result": {"results": []}}, PINNED.grid3_versions).status is u.Status.UNREADABLE


def test_map_reports_an_unpinned_travel_time_coverage():
    new = "Accessibility__202501_Global_Walking_Only_Travel_Time_To_Healthcare"
    finding = u.map_finding(_capabilities(*PINNED.map_coverages, new, "Accessibility__201501_Global_Travel_Time_to_Cities"),
                            PINNED.map_coverages)
    assert finding.status is u.Status.NEW_RELEASE
    assert new in finding.detail
    assert "Cities" not in finding.detail, "only healthcare travel-time coverages count"


def test_map_is_current_with_only_the_pinned_coverages():
    assert u.map_finding(_capabilities(*PINNED.map_coverages), PINNED.map_coverages).status is u.Status.CURRENT


def _pages(espen_body: str = "") -> dict:
    pages = {
        u.WORLDPOP_INDEX_URL: WORLDPOP_INDEX,
        u.HDX_SEARCH_URL: json.dumps(_hdx("grid3-nga-health-facilities-v3-0")),
        u.MAP_CAPABILITIES_URL: _capabilities(*PINNED.map_coverages),
        u.MAP_MALARIA_CAPABILITIES_URL: _capabilities(*MALARIA_LISTED),
    }
    for disease in u.ESPEN_DISEASES:
        pages[u.ESPEN_DOWNLOAD_URL.format(
            disease=disease, from_year=PINNED.espen_latest_year + 1, to_year=TODAY.year)] = espen_body
    return pages


def test_check_upstream_reports_every_publisher_current():
    findings = u.check_upstream(PINNED, get=_pages().__getitem__, today=TODAY)
    assert [f.source for f in findings] == [
        "WorldPop", "GRID3 health facilities", "Malaria Atlas Project", "Malaria Atlas Project malaria", "ESPEN"]
    assert [f.status for f in findings] == [u.Status.CURRENT] * 5


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
    assert finding.status is u.Status.NEW_RELEASE
    assert "2026" in finding.detail


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
            raise urllib.error.URLError("timed out")
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
    assert pinned.map_malaria_coverages == PINNED.map_malaria_coverages


def test_read_pinned_refuses_a_sources_file_without_a_worldpop_release(tmp_path: Path):
    path = tmp_path / "sources.yml"
    path.write_text("sources:\n  access_population_lga:\n    upstream:\n      worldpop: {}\n")
    with pytest.raises(ValueError, match="worldpop.release"):
        u.read_pinned(path)


def test_espen_years_rejects_a_non_numeric_year():
    with pytest.raises(ValueError, match="'N/A' is not a number"):
        u.espen_years(ESPEN_HEADER + "1,NG,1,N/A,x\n")


def test_a_reply_in_an_unexpected_format_is_unreadable_not_unreachable():
    body = ESPEN_HEADER + "1,NG,36337,N/A,x\n"
    finding = u.check_upstream(PINNED, get=_pages(body).__getitem__, today=TODAY)[-1]
    assert finding.status is u.Status.UNREADABLE, "the portal answered, so the problem is the format, not the network"
    assert "N/A" in finding.detail


def test_malformed_json_from_hdx_is_unreadable():
    pages = _pages()
    pages[u.HDX_SEARCH_URL] = "<html>not json</html>"
    findings = {f.source: f for f in u.check_upstream(PINNED, get=pages.__getitem__, today=TODAY)}
    assert findings["GRID3 health facilities"].status is u.Status.UNREADABLE


class _Body:
    def __init__(self, text):
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.text.encode()


def test_http_get_retries_a_refused_connection_then_succeeds(monkeypatch):
    replies = [urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")), _Body("ok")]

    def urlopen(*_args, **_kwargs):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    waits = []
    monkeypatch.setattr(u.urllib.request, "urlopen", urlopen)
    assert u.http_get("https://example.invalid", sleep=waits.append) == "ok"
    assert waits == [2.0], "one backoff before the second attempt"


def test_http_get_gives_up_after_its_attempts(monkeypatch):
    def urlopen(*_args, **_kwargs):
        raise urllib.error.URLError("down")

    waits = []
    monkeypatch.setattr(u.urllib.request, "urlopen", urlopen)
    with pytest.raises(urllib.error.URLError):
        u.http_get("https://example.invalid", sleep=waits.append)
    assert waits == [2.0, 4.0], "exponential backoff between three attempts"


def test_http_get_does_not_retry_an_http_error(monkeypatch):
    calls = []

    def urlopen(*_args, **_kwargs):
        calls.append(1)
        raise urllib.error.HTTPError("https://example.invalid", 404, "Not Found", {}, None)

    monkeypatch.setattr(u.urllib.request, "urlopen", urlopen)
    with pytest.raises(urllib.error.HTTPError):
        u.http_get("https://example.invalid", sleep=lambda _s: None)
    assert len(calls) == 1, "an HTTP error status is an answer, not a transient refusal"



def test_map_malaria_is_current_when_only_older_releases_are_listed_beside_the_pinned_one():
    finding = u.map_malaria_finding(_capabilities(*MALARIA_LISTED), PINNED.map_malaria_coverages)
    assert finding.status is u.Status.CURRENT, "older releases stay listed and must not read as new"
    assert "202608" in finding.detail


def test_map_malaria_reports_a_newer_release():
    listed = (*MALARIA_LISTED, "Malaria__202708_Global_Pf_Parasite_Rate", "Malaria__202708_Global_Pf_Incidence_Rate")
    finding = u.map_malaria_finding(_capabilities(*listed), PINNED.map_malaria_coverages)
    assert finding.status is u.Status.NEW_RELEASE
    assert "202708" in finding.detail and "202608" in finding.detail


def test_map_malaria_ignores_a_newer_release_of_a_product_the_register_does_not_use():
    listed = (*MALARIA_LISTED, "Malaria__202708_Global_Pv_Parasite_Rate", "Malaria__202708_Global_Pf_Mortality_Rate")
    finding = u.map_malaria_finding(_capabilities(*listed), PINNED.map_malaria_coverages)
    assert finding.status is u.Status.CURRENT


def test_map_malaria_without_the_products_is_unreadable_not_current():
    finding = u.map_malaria_finding(_capabilities("Malaria__202608_Global_Pv_Parasite_Rate"), PINNED.map_malaria_coverages)
    assert finding.status is u.Status.UNREADABLE
