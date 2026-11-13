"""Website-to-company tie and site news/jobs, from the 13 November 2026 reviewer feedback.

Two published websites belonged to someone else:

* ISABELLA CAFE AS (Ringerike) -> https://www.isabellacafe.com/, a restaurant in Tinley Park, Illinois;
* ABHO EIENDOM AS (Bergen) -> https://abho.com, a domain-for-sale page.

"Drop any claim that cannot be tied to the company's name or organisation number."
The fixtures below reproduce both pages (trimmed) and the positive cases.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent import enrichment
from norway_company_agent.enrichment import (
    apply_strict_site_gate,
    discover_website,
    enrich_from_site,
)
from norway_company_agent.evidence import evidence
from norway_company_agent.identity import apply_website_identity_gate
from norway_company_agent.quality import (
    article_page_date,
    careers_posting_links,
    listing_article_links,
    parse_text_date,
    posting_from_page,
    redact_unpublished,
    screen_job_posting,
    site_confidence,
)
from norway_company_agent.site_identity import printed_orgnrs, valid_orgnr, verify_site
from norway_company_agent.synthesis import build_claims
from norway_company_agent.website import parked_reason, visible_text

TODAY = date(2026, 11, 14)

ISABELLA_ROW = {
    "organisasjonsnummer": "932415313", "navn": "ISABELLA CAFE AS",
    "forretningsadresse.adresse": "Stabells gate 13", "forretningsadresse.postnummer": "3510",
    "forretningsadresse.poststed": "HØNEFOSS", "forretningsadresse.kommune": "RINGERIKE",
    "postadresse.adresse": "Sverdrups gate 3", "postadresse.postnummer": "0559", "postadresse.poststed": "OSLO",
    "postadresse.kommune": "OSLO",
}
ABHO_ROW = {
    "organisasjonsnummer": "917197792", "navn": "ABHO EIENDOM AS",
    "forretningsadresse.adresse": "Nøttveitvegen 236", "forretningsadresse.postnummer": "5238",
    "forretningsadresse.poststed": "RÅDAL", "forretningsadresse.kommune": "BERGEN",
}

# Trimmed copy of https://www.isabellacafe.com/ (fetched 2026-11-14).
ISABELLA_US_HTML = """<html><head><title>Home | Isabella's Cafe Italian Restaurant  | Tinley Park, IL</title>
<meta name="description" content="Isabella's Cafe, family Italian restaurant in Tinley Park, Illinois."></head>
<body><h1>Isabella's Cafe</h1><p>Authentic Italian food since 1998. Order online for pickup or delivery.</p>
<footer>Isabella's Cafe · 16846 Oak Park Ave, Tinley Park, IL 60477 · (708) 555-0136</footer></body></html>"""

# Copy of https://abho.com/ (fetched 2026-11-14): a NamePros for-sale frame.
ABHO_PARKED_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<link rel="canonical" href="https://www.namepros.com/parked/abho.com"><title>abho.com for sale</title>
<meta name="description" content="abho.com is for sale. Contact the seller through NamePros to ask the price or make an offer.">
<meta property="og:url" content="https://www.namepros.com/parked/abho.com"></head>
<body><iframe src="https://www.namepros.com/parked/abho.com?domain=abho.com&amp;iframe=1"></iframe></body></html>"""


def _profile(row: dict) -> dict:
    return {
        "organisation_number": row["organisasjonsnummer"],
        "name": row["navn"],
        "evidence": {"registry": evidence("registry", "available", "official_registry_bulk", "https://data.brreg.no/", value=row)},
    }


def _value(url: str, html: str, pages: list[tuple[str, str]] = ()) -> dict:
    soup = BeautifulSoup(html, "lxml")
    text = visible_text(soup)
    value = {
        "final_url": url,
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "description": (soup.select_one('meta[name="description"]') or {}).get("content", "") if soup.select_one('meta[name="description"]') else "",
        "site_text": text,
        "main_text_excerpt": text,
        "pages": [{"url": page_url, "title": "", "site_text": visible_text(page_html)} for page_url, page_html in pages],
        "social_links": [],
    }
    parked = parked_reason(soup, url, text)
    if parked:
        value["parked_reason"] = parked
    return value


def _record(url: str, html: str, pages: list[tuple[str, str]] = ()) -> dict:
    return evidence("website", "available", "registry_linked_company_website", url, value=_value(url, html, pages))


# --------------------------------------------------------------------------- the two wrong matches


def test_isabella_cafe_in_illinois_is_rejected_for_the_ringerike_company():
    profile = _profile(ISABELLA_ROW)
    value = _value("https://www.isabellacafe.com/", ISABELLA_US_HTML)
    for guessed in (True, False):
        assessment = verify_site(profile, value, guessed=guessed)
        assert assessment["publishable"] is False
        assert assessment["method"] == "foreign_tld_v1"
        assert assessment["signals"]["name"] is True  # the name matches; that is exactly the trap
    # Through the full gate pair, as the batch runs it.
    record = apply_website_identity_gate(profile, _record("https://www.isabellacafe.com/", ISABELLA_US_HTML))["website"]
    gated = apply_strict_site_gate(profile, record, guessed=False)
    assert gated["value"]["identity_assessment"]["publishable"] is False
    assert gated["value"]["social_links"] == []


def test_abho_domain_for_sale_page_is_rejected():
    profile = _profile(ABHO_ROW)
    value = _value("https://abho.com/", ABHO_PARKED_HTML)
    assert "namepros.com" in value["parked_reason"]
    assessment = verify_site(profile, value, guessed=True)
    assert assessment["publishable"] is False and assessment["method"] == "parked_page_v1"


def test_parking_markers_without_a_parking_host():
    for title in ("example.no is for sale", "This domain may be for sale!", "Domenet er til salgs", "Welcome to nginx!"):
        soup = BeautifulSoup(f"<html><head><title>{title}</title></head><body></body></html>", "lxml")
        assert parked_reason(soup, "https://example.no/", visible_text(soup)), title
    sedo = BeautifulSoup('<html><body><a href="https://sedo.com/search/details/?domain=x.no">Buy</a></body></html>', "lxml")
    assert parked_reason(sedo, "https://x.no/", "") is not None
    real = BeautifulSoup("<html><head><title>Bakeriet AS</title></head><body>Vi selger brød. Org.nr 923 609 016</body></html>", "lxml")
    assert parked_reason(real, "https://bakeriet.no/", visible_text(real)) is None


# --------------------------------------------------------------------------- what is accepted


def test_org_number_on_a_contact_page_ties_even_a_com_domain():
    profile = _profile(ISABELLA_ROW)
    home = "<html><head><title>Isabella Cafe</title></head><body>Kafé i sentrum.</body></html>"
    contact = "<html><body><h1>Kontakt</h1><p>Isabella Cafe AS · Org.nr: 932 415 313 MVA</p></body></html>"
    value = _value("https://isabellacafe.com/", home, [("https://isabellacafe.com/kontakt", contact)])
    assessment = verify_site(profile, value, guessed=True)
    assert assessment["publishable"] and assessment["method"] == "orgnr_on_site_v3"
    assert site_confidence({"identity_assessment": assessment}) == 0.97


def test_org_number_print_variants_are_recognised():
    profile = _profile(ISABELLA_ROW)
    for printed in ("932415313", "932 415 313", "932.415.313", "NO 932 415 313 MVA", "Foretaksregisteret NO932415313MVA"):
        value = _value("https://isabella.no/", f"<html><body><footer>{printed}</footer></body></html>")
        assert verify_site(profile, value, guessed=True)["signals"]["orgnr"], printed


def test_name_with_registry_address_ties_a_guessed_domain():
    profile = _profile(ISABELLA_ROW)
    html = "<html><head><title>Isabella Cafe - Hønefoss</title></head><body>Velkommen! Stabells gate 13, 3510 Hønefoss. Tlf +47 32 00 00 00</body></html>"
    assessment = verify_site(profile, _value("https://isabellacafe.no/", html), guessed=True)
    assert assessment["publishable"] and assessment["method"] == "name_and_registry_address_v1"
    assert assessment["signals"]["plus47"] is True
    assert site_confidence({"identity_assessment": assessment}) == 0.92


def test_name_only_is_enough_for_a_registry_listed_no_site_but_never_for_a_guess():
    profile = _profile(ISABELLA_ROW)
    html = "<html><head><title>Isabella Cafe</title></head><body>Kaffe, kaker og lunsj.</body></html>"
    value = _value("https://isabellacafe.no/", html)
    assert verify_site(profile, value, guessed=False)["publishable"] is True
    guessed = verify_site(profile, value, guessed=True)
    assert guessed["publishable"] is False and guessed["method"] == "guessed_name_only_v1"


def test_a_site_printing_another_org_number_as_its_own_is_rejected():
    profile = _profile(ISABELLA_ROW)
    html = "<html><head><title>Isabella Cafe</title></head><body>Isabella Cafe Bergen AS · Org.nr. 923 609 016</body></html>"
    assessment = verify_site(profile, _value("https://isabellacafe.no/", html), guessed=False)
    assert assessment["publishable"] is False and assessment["method"] == "other_orgnr_v1"


def test_org_number_checksum_filters_phone_numbers():
    assert valid_orgnr("932415313") and valid_orgnr("917197792") and valid_orgnr("923609016")
    assert not valid_orgnr("932415314") and not valid_orgnr("123456789")
    assert printed_orgnrs("Ring 932 415 314 eller org.nr 923 609 016") == [("923609016", True)]


def test_strict_gate_never_promotes_what_the_name_heuristic_rejected():
    profile = _profile(ISABELLA_ROW)
    # Registry address on the page, but the legal name is missing: neither gate publishes it.
    html = "<html><head><title>Lunsj og kaffe</title></head><body>Stabells gate 13, 3510 Hønefoss</body></html>"
    record = apply_website_identity_gate(profile, _record("https://lunsj.no/", html))["website"]
    gated = apply_strict_site_gate(profile, record, guessed=False)
    assert gated["value"]["identity_assessment"]["publishable"] is False


# --------------------------------------------------------------------------- discovery end to end (no network)


def test_discovery_rejects_isabella_com_and_keeps_no_candidate_url(monkeypatch):
    profile = _profile(ISABELLA_ROW)
    monkeypatch.setattr(enrichment, "_resolves", lambda host: host.endswith("isabellacafe.com"))

    def fake_fetch(url, **kwargs):
        return _record("https://www.isabellacafe.com/", ISABELLA_US_HTML), {"requests": 2, "bytes": 100, "latencies_ms": [5]}

    monkeypatch.setattr(enrichment, "fetch_website", fake_fetch)
    metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
    assert discover_website(profile, metrics) is None
    profile["evidence"]["website"] = evidence("website", "not_found", "registry_linked_company_website", "", note="No valid registry website URL")
    redact_unpublished(profile)
    dumped = json.dumps(profile, ensure_ascii=False)
    assert "isabellacafe" not in dumped
    assert profile["enrichment_trace"]["website_discovery"][-1]["result"] == "foreign_tld_v1"


def test_discovery_publishes_a_guess_with_the_org_number(monkeypatch):
    profile = _profile(ISABELLA_ROW)
    monkeypatch.setattr(enrichment, "_resolves", lambda host: host.endswith("isabellacafe.no"))
    home = "<html><head><title>Isabella Cafe</title></head><body>Kafé. Org nr 932415313</body></html>"
    monkeypatch.setattr(enrichment, "fetch_website", lambda url, **kwargs: (_record("https://isabellacafe.no/", home), {"requests": 2, "bytes": 1, "latencies_ms": []}))
    found = discover_website(profile, {"requests": 0, "bytes": 0, "latencies_ms": []})
    assert found and found["value"]["identity_assessment"]["method"] == "orgnr_on_site_v3"
    assert found["source_type"] == "discovered_company_website"


# --------------------------------------------------------------------------- redaction of unpublished URLs


def test_withheld_website_leaves_no_url_in_claims_or_profile():
    profile = _profile(ABHO_ROW)
    record = apply_website_identity_gate(profile, _record("https://abho.com/", ABHO_PARKED_HTML))["website"]
    profile["evidence"]["website"] = apply_strict_site_gate(profile, record, guessed=False)
    profile["evidence"]["hiring"] = evidence("hiring", "not_found", "company_owned_site", "https://abho.com/", note="no exact-identity company website to read from")
    redact_unpublished(profile)
    claims, rows, _ = build_claims(profile)
    blob = json.dumps({"claims": claims, "evidence": rows, "profile": profile}, ensure_ascii=False)
    assert "abho.com" not in blob
    website_claim = next(item for item in claims if item["field"] == "official_website")
    assert website_claim["availability"] == "ambiguous" and website_claim["value"] is None
    assert "sale" in website_claim["reason"] or "parking" in website_claim["reason"]


# --------------------------------------------------------------------------- news and jobs from the verified site


def test_text_dates_and_labelled_article_dates():
    assert parse_text_date("12.10.2026") == "2026-10-12"
    assert parse_text_date("3. mars 2026 kl 10") == "2026-03-03"
    assert parse_text_date("3 March 2026") == "2026-03-03"
    assert parse_text_date("31.02.2026") is None
    html = "<html><body><article><h1>Ny avtale med kommunen</h1><p>Publisert 12.10.2026</p><p>Tekst.</p></article></body></html>"
    assert article_page_date(html) == ("2026-10-12", None)
    assert article_page_date("<html><body><main><h1>Om oss</h1><p>© 2026</p></main></body></html>")[0] is None


def test_listing_links_stay_on_site_and_under_the_listing_path():
    html = """<html><body>
      <a href="/nyheter/ny-avtale-med-kommunen">Ny avtale med Ringerike kommune</a>
      <a href="/nyheter/page/2">Eldre innlegg</a>
      <a href="/nyheter/kategori/drift">Drift</a>
      <a href="https://other.no/nyheter/x">Ekstern sak om noe annet</a>
      <a href="/om-oss">Om oss</a>
    </body></html>"""
    links = listing_article_links("https://bakeriet.no/nyheter", html, site_url="https://bakeriet.no/")
    assert [item["url"] for item in links] == ["https://bakeriet.no/nyheter/ny-avtale-med-kommunen"]
    assert links[0]["published"] is None and links[0]["method"] == "listing_link"


def test_job_postings_need_a_title_and_an_open_date():
    assert screen_job_posting({"title": "Kokk"}, today=TODAY) == "posting has no date or deadline"
    assert screen_job_posting({"title": "Kokk", "posted": "2026-10-01"}, today=TODAY) is None
    assert "may be closed" in screen_job_posting({"title": "Kokk", "posted": "2025-01-01"}, today=TODAY)
    open_page = "<html><head><title>Kokk</title></head><body><main><h1>Kokk til kjøkkenet</h1><p>Søknadsfrist: 01.12.2026</p></main></body></html>"
    job, reason = posting_from_page(open_page, "https://bakeriet.no/karriere/kokk", today=TODAY)
    assert reason is None and job["title"] == "Kokk til kjøkkenet" and job["valid_through"] == "2026-12-01"
    closed = open_page.replace("01.12.2026", "01.10.2026")
    assert posting_from_page(closed, "https://bakeriet.no/karriere/kokk", today=TODAY) == (None, "posting closed on 2026-10-01")
    rolling = open_page.replace("01.12.2026", "snarest")
    assert posting_from_page(rolling, "https://bakeriet.no/karriere/kokk", today=TODAY) == (None, "posting has no date or deadline")
    careers = '<html><body><a href="/karriere/kokk">Kokk til kjøkkenet</a><a href="/kontakt">Kontakt</a></body></html>'
    assert [item["url"] for item in careers_posting_links("https://bakeriet.no/karriere", careers, site_url="https://bakeriet.no/")] == ["https://bakeriet.no/karriere/kokk"]


def test_enrich_from_site_publishes_dated_news_and_open_posting(monkeypatch):
    site = {
        "https://bakeriet.no/": '<html><body><a href="/nyheter">Nyheter</a><a href="/karriere">Karriere</a></body></html>',
        "https://bakeriet.no/nyheter": '<html><head><title>Nyheter</title></head><body><a href="/nyheter/ny-ovn">Vi har fått ny steinovn i bakeriet</a><a href="/nyheter/hello-world">Hello world!</a></body></html>',
        "https://bakeriet.no/nyheter/ny-ovn": '<html><head><meta property="og:title" content="Vi har fått ny steinovn"><meta property="article:published_time" content="2026-10-20T08:00:00+02:00"></head><body><h1>Ny ovn</h1></body></html>',
        "https://bakeriet.no/nyheter/hello-world": '<html><head><meta property="article:published_time" content="2026-01-01"></head><body><h1>Hello world!</h1></body></html>',
        "https://bakeriet.no/karriere": '<html><head><title>Karriere</title></head><body><h1>Ledige stillinger</h1><a href="/karriere/baker">Baker, fast stilling</a></body></html>',
        "https://bakeriet.no/karriere/baker": '<html><body><main><h1>Baker, fast stilling</h1><p>Søknadsfrist: 01.12.2099</p></main></body></html>',
    }

    def fake_get(url, **kwargs):
        clean = url if url in site else url.rstrip("/")
        return {"url": clean, "content_type": "text/html", "raw": site[clean].encode(), "sha256": "x" * 64, "elapsed_ms": 1, "retrieved_at": "2026-11-14T00:00:00Z"}

    monkeypatch.setattr(enrichment, "_get", fake_get)
    monkeypatch.setattr(enrichment, "robots_check", lambda url, timeout: (True, 0))
    website = evidence("website", "available", "registry_linked_company_website", "https://bakeriet.no/", value={"final_url": "https://bakeriet.no/", "identity_assessment": {"publishable": True}})
    out = enrich_from_site({"organisation_number": "923609016", "name": "Bakeriet AS"}, website, {"requests": 0, "bytes": 0, "latencies_ms": []})
    activity = out["public_activity"]
    assert activity["status"] == "available"
    assert [(item["title"], item["published"]) for item in activity["value"]["items"]] == [("Vi har fått ny steinovn", "2026-10-20")]
    assert any("default CMS" in item["reason"] for item in activity["value"]["withheld"])
    hiring = out["hiring"]
    assert hiring["status"] == "available"
    assert [(job["title"], job["valid_through"]) for job in hiring["value"]["job_postings"]] == [("Baker, fast stilling", "2099-12-01")]


def test_links_with_norwegian_letters_are_requestable():
    assert enrichment._ascii_url("https://bakeriet.no/nyheter/ny-båt?s=ø") == "https://bakeriet.no/nyheter/ny-b%C3%A5t?s=%C3%B8"
    assert enrichment._ascii_url("https://www.minkrypto.no/post/s%C3%A5nn") == "https://www.minkrypto.no/post/s%C3%A5nn"


def test_booking_page_is_not_a_careers_page():
    soup = BeautifulSoup('<a href="/bestillinger/">Bestill time</a><a href="/ledige-stillinger/">Jobb hos oss</a>', "lxml")
    assert enrichment._same_site_links("https://lege.no/", soup, enrichment.CAREER_TERMS, 5) == ["https://lege.no/ledige-stillinger/"]


def test_pages_are_decoded_in_their_declared_charset():
    page = {"raw": "Sammenlign prisen på dekkpakken før".encode("iso-8859-1"), "content_type": "text/html; charset=ISO-8859-1"}
    assert enrichment._decode(page) == "Sammenlign prisen på dekkpakken før"
    meta = {"raw": b'<meta charset="windows-1252">' + "på".encode("cp1252"), "content_type": "text/html"}
    assert enrichment._decode(meta).endswith("på")
