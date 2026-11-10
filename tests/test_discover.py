"""Bylaw candidate finder, fully offline: fetch and web search are monkeypatched to serve fixture pages."""
from __future__ import annotations

import io
import json

import pytest

from reservoir.bylaws import discover as D
from reservoir.bylaws import tracker

pytestmark = pytest.mark.unit


def page(title: str, body: str) -> bytes:
    return f"<!doctype html><html><head><title>{title}</title></head><body><h1>{title}</h1><p>{body}</p></body></html>".encode()


def ok(url, body: bytes, ctype="text/html; charset=utf-8"):
    return D.Fetched(url=url, status=200, content_type=ctype, body=body, retrieved="2026-11-07T12:00:00")


@pytest.fixture
def web(tmp_path, monkeypatch):
    """Isolated tracker/cache/queue dirs; fetch serves `pages`, everything else fails like an unknown host."""
    monkeypatch.setattr(tracker, "TRACKER", tmp_path / "bylaws" / "tracker.jsonl")
    monkeypatch.setattr(tracker, "upsert", lambda *a, **k: pytest.fail("discover must never write the tracker"))
    monkeypatch.setattr(D, "web_search", lambda q, count=8: pytest.fail("no web search without BRAVE_SEARCH_API_KEY"))
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    pages: dict[str, D.Fetched] = {}
    calls: list[str] = []

    def fake_fetch(url, refresh=False):
        calls.append(url)
        return pages.get(url) or D.Fetched(url=url, status=None, error="ConnectionError: unknown host", retrieved="2026-11-07T12:00:00")
    monkeypatch.setattr(D, "fetch", fake_fetch)
    return pages, calls, tmp_path


CORTLANDT = "https://www.townofcortlandtny.gov/documents/LL-2026-01.pdf"
CORTLANDVILLE = "https://www.cortlandville.gov/DocumentCenter/View/700/Local-Law-3-2022-BESS"


def test_cortlandt_never_matches_a_cortlandville_document(web):
    pages, _, _ = web
    pages[CORTLANDVILLE] = ok(CORTLANDVILLE, page("Town of Cortlandville Local Law No. 3 of 2022 - Battery Energy Storage System Law",
                                                  "This chapter shall be known as the Battery Energy Storage System Law of the Town of Cortlandville."))
    pages[CORTLANDT] = ok(CORTLANDT, page("Town of Cortlandt Local Law No. 1 of 2026 - Battery Storage Moratorium",
                                          "The moratorium herein shall be extended through December 31, 2026."))
    res = D.discover("NY", "Cortlandt", "Westchester", [CORTLANDVILLE, CORTLANDT], progress=lambda m: None)
    assert res.candidates[0].url == CORTLANDT
    assert res.candidates[0].municipality_match == "exact" and res.candidates[0].kind == "local_law"
    cv = next(c for c in res.rejects if c.url == CORTLANDVILLE)
    assert cv.municipality_match == "mismatch" and cv.reject_reason == "names a different municipality"
    assert all(c.url != CORTLANDVILLE for c in res.candidates + res.more)
    assert all('"Westchester County" New York' in q for q in res.queries)


def test_confusable_alone_is_a_mismatch_not_likely():
    assert D.municipality_match("Cortlandt", "Battery storage law", "Adopted by the Cortlandville Town Board", "https://x.org/a") == "mismatch"
    assert D.municipality_match("Cortlandville", "Town of Cortlandt Local Law", "", "https://x.org/a") == "mismatch"
    assert D.municipality_match("Town of Uxbridge", "Uxbridge Zoning Bylaw", "", "https://x.org/a") == "likely"
    assert D.municipality_match("Red Hook", "Town of Red Hook Zoning Law", "", "https://x.org/a") == "exact"


UX_CODE = "https://ecode360.com/UX0001"
UX_PROPOSED = "https://www.uxbridge-ma.gov/DocumentCenter/View/55/Proposed-Solar-Overlay"
UX_MODEL = "https://www.mass.gov/doc/solar-pv-battery-energy-storage-system-bess-model-bylaws/download"
UX_NEWS = "https://www.mma.org/news/towns-weigh-solar-bylaws"


def test_drafts_model_bylaws_and_news_rank_below_living_code(web):
    pages, _, _ = web
    pages[UX_CODE] = ok(UX_CODE, page("Town of Uxbridge, MA Zoning Bylaw", "Article IX Large-scale ground-mounted solar photovoltaic installations."))
    pages[UX_PROPOSED] = ok(UX_PROPOSED, page("Proposed Zoning Bylaw Amendment - Solar Overlay District", "Town of Uxbridge draft for Town Meeting."))
    pages[UX_MODEL] = ok(UX_MODEL, page("Model Solar Bylaw", "DOER model solar PV bylaw for Massachusetts municipalities."))
    pages[UX_NEWS] = ok(UX_NEWS, page("Towns weigh new solar bylaws", "Uxbridge and other towns are reviewing solar rules."))
    res = D.discover("MA", "Uxbridge", "Worcester", [UX_NEWS, UX_MODEL, UX_PROPOSED, UX_CODE], progress=lambda m: None)
    ranked = res.candidates + res.more
    assert ranked[0].url == UX_CODE and ranked[0].kind == "living_code" and ranked[0].rank == 1
    rej = {c.url: c for c in res.rejects}
    assert rej[UX_PROPOSED].kind == "proposed" and "not adopted" in rej[UX_PROPOSED].reject_reason
    assert rej[UX_MODEL].kind == "model_bylaw"
    news = next(c for c in ranked if c.url == UX_NEWS)
    assert news.kind == "news" and news.rank == len(ranked) and not news.extractable
    assert res.queries[0].startswith('"Uxbridge" "zoning bylaw"')        # MA: bylaw before ordinance
    with pytest.raises(ValueError, match="news"):
        D.pick("MA", "Uxbridge", news.rank)


def test_403_and_404_are_recorded_not_raised(web):
    pages, _, _ = web
    a, b = "https://www.townofwaterfordny.gov/ll-1-2026", "https://www.cortlandville.gov/DocumentCenter/View/864/Proposed"
    pages[a] = D.Fetched(url=a, status=403, retrieved="2026-11-07T12:00:00")
    pages[b] = D.Fetched(url=b, status=404, retrieved="2026-11-07T12:00:00")
    res = D.discover("NY", "Waterford", "Saratoga", [a, b], progress=lambda m: None)
    rej = {c.url: c.reject_reason for c in res.rejects}
    assert rej[a] == "HTTP 403" and rej[b] == "HTTP 404"
    assert any("unknown host" in c.reject_reason for c in res.rejects)   # guessed hosts that don't exist
    assert res.candidates == []


def test_scanned_pdf_is_flagged_and_never_ranked(web):
    from pypdf import PdfWriter

    pages, _, _ = web
    buf = io.BytesIO()
    w = PdfWriter()
    w.add_blank_page(612, 792)
    w.write(buf)
    url = "https://www.suttonma.org/DocumentCenter/View/9/Zoning-Bylaw-1998.pdf"
    pages[url] = ok(url, buf.getvalue(), "application/pdf")
    res = D.discover("MA", "Sutton", "", [url], progress=lambda m: None)
    c = next(c for c in res.rejects if c.url == url)
    assert c.scanned and "OCR" in c.reject_reason


def test_town_site_links_followed_one_hop(web):
    pages, calls, _ = web
    home = "https://www.douglasma.org/"
    pdf = "https://www.douglasma.org/DocumentCenter/View/12/Zoning-Bylaw-2025"
    pages[home] = ok(home, b"<html><head><title>Town of Douglas, MA</title></head><body>"
                           b"<a href='/DocumentCenter/View/12/Zoning-Bylaw-2025'>Zoning Bylaw (2025)</a>"
                           b"<a href='/parks'>Parks</a><a href='mailto:x@y.z'>mail</a></body></html>")
    pages[pdf] = ok(pdf, page("Town of Douglas Zoning Bylaw 2025", "Section 7 Solar photovoltaic installations."))
    res = D.discover("MA", "Douglas", "Worcester", progress=lambda m: None)
    assert pdf in calls and "https://www.douglasma.org/parks" not in calls
    top = res.candidates[0]
    assert top.url == pdf and top.source == "link" and top.municipality_match == "exact"


def test_queue_written_tracker_untouched_and_rerun_notes_new_urls(web):
    pages, _, tmp = web
    pages[UX_CODE] = ok(UX_CODE, page("Town of Uxbridge, MA Zoning Bylaw", "Solar."))
    D.discover("MA", "Uxbridge", "", [UX_CODE], progress=lambda m: None)
    q = D.queue_path("MA", "Uxbridge")
    assert q.exists() and q.parent == tmp / "bylaws" / "discover"
    assert not tracker.TRACKER.exists()
    first = json.loads(q.read_text())
    assert first["previous_run"] is None and first["new_urls"] == []
    extra = "https://www.uxbridge-ma.gov/DocumentCenter/View/77/Solar-Bylaw-2024"
    pages[extra] = ok(extra, page("Town of Uxbridge Solar Bylaw 2024", "Ground-mounted solar."))
    res = D.discover("MA", "Uxbridge", "", [UX_CODE, extra], progress=lambda m: None)
    assert res.previous_run == first["retrieved"] and res.new_urls == [extra]
    assert not tracker.TRACKER.exists()


def test_extract_from_discover_uses_saved_queue_url(web, monkeypatch):
    from reservoir import cli
    from reservoir.bylaws import extract

    pages, _, _ = web
    pages[UX_CODE] = ok(UX_CODE, page("Town of Uxbridge, MA Zoning Bylaw", "Solar."))
    D.discover("MA", "Uxbridge", "", [UX_CODE], progress=lambda m: None)
    got = {}

    def fake_extract(state, town, source, county=""):
        got.update(state=state, town=town, source=source)
        from reservoir.bylaws.schema import TownRecord
        return TownRecord(state=state, municipality=town), None
    monkeypatch.setattr(extract, "extract_to_tracker", fake_extract)
    cli.main(["bylaws", "extract", "--state", "MA", "--town", "Uxbridge", "--from-discover", "1"])
    assert got == {"state": "MA", "town": "Uxbridge", "source": UX_CODE}
    with pytest.raises(SystemExit):
        cli.main(["bylaws", "extract", "--state", "MA", "--town", "Uxbridge", "--from-discover", "1", "--source", "x.pdf"])


def test_queries_and_patterns_are_plain_and_bounded():
    ny = D.build_queries("NY", "Town of Waterford", "Saratoga", year=2026)
    assert any('"Local Law" 2026' in q for q in ny) and all("New York" in q and "Saratoga" in q for q in ny)
    assert D.host_guesses("NY", "Cortlandt")[0] == "www.townofcortlandtny.gov"
    assert D.norm_url("HTTPS://WWW.Ecode360.com/UX0001/") == D.norm_url("https://ecode360.com/UX0001")


def test_search_index_and_login_pages_are_not_candidates(web):
    pages, _, _ = web
    s = "https://ecode360.com/search?query=Uxbridge+Massachusetts"
    pages[s] = ok(s, page("Login - eCode360", "Please sign in."))
    res = D.discover("MA", "Uxbridge", "", [s], progress=lambda m: None)
    assert next(c for c in res.rejects if c.url == s).reject_reason.startswith("search, index or login page")


def test_second_hop_stays_on_the_town_host(web):
    pages, calls, _ = web
    home = "https://www.uxbridge-ma.gov/"
    idx = "https://www.uxbridge-ma.gov/298/Town-Charter-By-Laws"
    pdf = "https://www.uxbridge-ma.gov/DocumentCenter/View/5385/Zoning-By-Laws"
    off = "https://elsewhere.example.com/zoning-bylaw.pdf"
    pages[home] = ok(home, b"<html><head><title>Uxbridge, MA</title></head><body><a href='/298/Town-Charter-By-Laws'>Town Charter &amp; By-Laws</a></body></html>")
    pages[idx] = ok(idx, b"<html><head><title>Town Charter &amp; By-Laws | Uxbridge, MA</title></head><body>"
                         b"<a href='/DocumentCenter/View/5385/Zoning-By-Laws'>Zoning By-Laws</a>"
                         b"<a href='https://elsewhere.example.com/zoning-bylaw.pdf'>Zoning bylaw mirror</a></body></html>")
    pages[pdf] = ok(pdf, page("TOWN OF UXBRIDGE ZONING BY-LAWS", "Section 400 Solar."), "application/pdf; charset=binary")
    res = D.discover("MA", "Uxbridge", "", progress=lambda m: None)
    assert pdf in calls and off not in calls
    assert res.candidates[0].url == pdf and res.candidates[0].municipality_match == "exact"


def test_corrupt_pdf_is_a_reject_not_a_crash(web):
    pages, _, _ = web
    url = "https://www.suttonma.org/DocumentCenter/View/10/Broken.pdf"
    pages[url] = ok(url, b"%PDF-1.7 truncated garbage", "application/pdf")
    res = D.discover("MA", "Sutton", "", [url], progress=lambda m: None)
    c = next(c for c in res.rejects if c.url == url)
    assert c.reject_reason and all(x.url != url for x in res.candidates)   # rejected; the run carried on


def test_bonuses_reorder_within_a_tier_never_across():
    mk = lambda **kw: D.Candidate(url="https://x/" + kw.get("title", ""), source="seed", **kw)
    code = mk(kind="living_code", title="Table of Contents - Town of Cortlandt", municipality_match="unknown")
    pdf = mk(kind="clerk_pdf", title="Zoning district attachment 2026", year=2026, municipality_match="exact")
    law = mk(kind="local_law", title="Local Law No. 1 of 2026 Solar Moratorium", year=2026, municipality_match="exact")
    old_pdf = mk(kind="clerk_pdf", title="Solar zoning bylaw 1998", year=1998, municipality_match="exact")
    html = mk(kind="unknown", title="Zoning Board", municipality_match="exact")
    news = mk(kind="news", title="Zoning solar moratorium news", municipality_match="exact")
    order = sorted([news, html, old_pdf, law, pdf, code], key=lambda c: -D.score(c, this_year=2026))
    assert [c.kind for c in order] == ["living_code", "clerk_pdf", "local_law", "clerk_pdf", "unknown", "news"]
    assert order[1] is pdf and order[3] is old_pdf
