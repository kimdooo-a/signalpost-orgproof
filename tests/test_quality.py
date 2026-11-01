"""Publication gates from the 1 November 2026 reviewer feedback.

1. News: no "Hello world!" posts, no undated or static pages — only dated articles.
2. Jobs: no login-only applicant-system dashboards, no vendor homepages.
3. Social: no group / parent / sister pages.
Plus: a per-company confidence level and a one-line reason for every blank.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.enrichment import _parse_feed
from norway_company_agent.quality import (
    apply_batch_consistency,
    classify_ats_link,
    group_members,
    is_comment_feed,
    screen_careers_page,
    screen_job_posting,
    screen_news,
    screen_news_item,
    screen_social_link,
)
from norway_company_agent.synthesis import build_claims, build_summary

TODAY = date(2026, 11, 2)
SITE = "https://example.no/"


# ------------------------------------------------------------------ news


def test_wordpress_hello_world_is_not_news_even_with_a_date():
    item = {"title": "Hello world!", "url": "https://example.no/2024/01/hello-world/", "published": "2024-01-03"}
    assert screen_news_item(item, site_url=SITE, today=TODAY) == "default CMS or static-page title, not news"
    assert screen_news_item({**item, "title": "Hei verden!"}, site_url=SITE, today=TODAY)


def test_undated_or_static_pages_are_withheld_with_a_reason():
    assert screen_news_item({"title": "Om oss", "url": "https://example.no/om-oss/"}, site_url=SITE, today=TODAY) == "page has no date"
    assert screen_news_item({"title": "Våre tjenester", "url": "https://example.no/tjenester/", "published": "2025-02-01"}, site_url=SITE, today=TODAY) == "static page, not a news article"
    # A date read off the homepage itself is the page's date, not an article's.
    assert screen_news_item({"title": "Velkommen til Eksempel", "url": SITE, "published": "2025-02-01"}, site_url=SITE, today=TODAY) == "item points at the homepage, not an article"
    assert screen_news_item({"title": "Siste nytt", "url": "https://example.no/nyheter/", "published": "2025-02-01"}, site_url=SITE, read_from=[SITE, "https://example.no/nyheter"], today=TODAY) == "date belongs to a listing or static page, not an article"


def test_real_dated_article_on_the_company_site_is_kept():
    item = {"title": "Ny rammeavtale med Statens vegvesen", "url": "https://example.no/nyheter/ny-rammeavtale", "published": "2026-09-01"}
    assert screen_news_item(item, site_url=SITE, read_from=[SITE, "https://example.no/nyheter"], today=TODAY) is None


def test_off_site_future_and_comment_items_are_withheld():
    assert screen_news_item({"title": "Bransjenytt", "url": "https://other.no/a", "published": "2026-01-01"}, site_url=SITE, today=TODAY) == "item links off the company's own site"
    assert screen_news_item({"title": "Lansering", "url": "https://example.no/a/lansering", "published": "2027-05-01"}, site_url=SITE, today=TODAY) == "date is in the future"
    assert screen_news_item({"title": "Comment on Hello world! by A WordPress Commenter", "url": "https://example.no/2024/01/hello-world/#comment-1", "published": "2024-01-03"}, site_url=SITE, today=TODAY)
    assert is_comment_feed("https://example.no/comments/feed/")
    assert not is_comment_feed("https://example.no/feed/")


def test_screen_news_splits_kept_and_withheld():
    kept, withheld = screen_news(
        [
            {"title": "Hello world!", "url": "https://example.no/hello-world/", "published": "2024-01-03"},
            {"title": "Vi har flyttet", "url": "https://example.no/nyheter/vi-har-flyttet", "published": "2026-03-04"},
        ],
        site_url=SITE,
        today=TODAY,
    )
    assert [item["title"] for item in kept] == ["Vi har flyttet"]
    assert withheld[0]["reason"] == "default CMS or static-page title, not news"


def test_atom_replies_link_is_not_taken_as_the_article_url():
    raw = b"""<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'>
    <entry><title>Ny avtale</title><link rel='alternate' href='https://example.no/nyheter/ny-avtale'/>
    <link rel='replies' href='https://example.no/nyheter/ny-avtale#comments'/><published>2026-09-01T10:00:00Z</published></entry></feed>"""
    assert _parse_feed(raw, "https://example.no/feed/") == [{"title": "Ny avtale", "url": "https://example.no/nyheter/ny-avtale", "published": "2026-09-01"}]


# ------------------------------------------------------------------ jobs


def test_teamtailor_dashboard_and_homepage_are_not_hiring_evidence():
    assert classify_ats_link("https://app.teamtailor.com/companies/abc123/jobs") == "login-only applicant-system dashboard"
    assert classify_ats_link("https://www.teamtailor.com/?utm_source=career-site&utm_medium=footer") is not None
    assert classify_ats_link("https://teamtailor.com/") is not None
    assert classify_ats_link("https://www.teamtailor.com/en/", "Powered by Teamtailor") == "vendor 'powered by' link, not a job board"
    assert classify_ats_link("https://eksempel.teamtailor.com/users/sign_in") == "login-only applicant-system dashboard"


def test_company_job_boards_and_postings_are_kept():
    assert classify_ats_link("https://eksempel.teamtailor.com/jobs") is None
    assert classify_ats_link("https://eksempel.teamtailor.com/") is None
    assert classify_ats_link("https://candidate.webcruiter.com/nb-no/eksempel/") is None
    assert classify_ats_link("https://www.jobbnorge.no/ledige-stillinger/stilling/123456/ingenior") is None
    assert classify_ats_link("https://www.webcruiter.no/") is not None
    assert classify_ats_link("https://www.jobbnorge.no/") is not None


def test_careers_page_must_be_the_companys_own_public_jobs_page():
    page = {"url": "https://example.no/karriere", "title": "Karriere – Eksempel AS"}
    assert screen_careers_page(page, site_url=SITE) is None
    assert screen_careers_page({"url": SITE, "title": "Eksempel"}, site_url=SITE) == "careers link redirects to the homepage"
    assert screen_careers_page({"url": "https://example.no/min-side/login", "title": "Logg inn"}, site_url=SITE) == "login-only page, not a public careers page"
    assert screen_careers_page({"url": "https://example.no/bli-med", "title": "Bli med"}, site_url=SITE, body_text="Vi selger sykler", has_password_field=False) == "page is not about this company's own vacancies"
    assert screen_careers_page({"url": "https://example.no/bli-med", "title": "Bli med"}, site_url=SITE, body_text="Se våre ledige stillinger") is None
    # A recruitment agency's service page is not that agency's own careers page.
    assert screen_careers_page({"url": "https://www.linkrekruttering.no/rekruttering/", "title": "Rekruttering - Link"}, site_url="https://www.linkrekruttering.no/", body_text="Vi hjelper deg å finne riktig kandidat") == "page is not about this company's own vacancies"
    assert screen_careers_page({"url": "https://example.no/bli-med", "title": "Bli med"}, site_url=SITE, has_password_field=True) == "login-only page, not a public careers page"


def test_closed_postings_are_withheld():
    assert screen_job_posting({"title": "Elektriker", "valid_through": "2026-10-01"}, today=TODAY) == "posting closed on 2026-10-01"
    assert screen_job_posting({"title": "Elektriker", "valid_through": "2026-12-01"}, today=TODAY) is None
    assert screen_job_posting({"title": ""}, today=TODAY) == "posting has no title"


# ------------------------------------------------------------------ social


def test_group_facebook_page_is_not_the_subsidiarys_page():
    # The shared brand without the subsidiary's own name is a group or brand page.
    assert screen_social_link({"platform": "facebook", "url": "https://facebook.com/nordicelektro"}, legal_name="NORDIC ELEKTRO BERGEN AS") == "handle matches only part of the legal name; may be a group or brand page"
    assert screen_social_link({"platform": "facebook", "url": "https://facebook.com/nordicelektrobergengroup"}, legal_name="NORDIC ELEKTRO BERGEN AS") == "handle names a group ('group'), not this company"


def test_handle_naming_another_group_company_names_that_company():
    group_value = {
        "navn": "FJELLGLØD HOLDING AS", "organisasjonsnummer": "935095190",
        "children": [{"navn": "FJELLGLØD HUD OG VELVÆRE AS", "organisasjonsnummer": "936326005", "parentNavn": "FJELLGLØD HOLDING AS", "parentOrganisasjonsnummer": "935095190"}],
    }
    group = group_members(group_value, "936326005")
    assert group == [{"organisation_number": "935095190", "name": "FJELLGLØD HOLDING AS"}]
    reason = screen_social_link({"platform": "facebook", "url": "https://facebook.com/fjellglodholding"}, legal_name="FJELLGLØD HUD OG VELVÆRE AS", group=group)
    assert reason == "profile belongs to group company FJELLGLØD HOLDING AS (935095190)"
    # The parent publishing its subsidiary's page is the same mistake in the other direction.
    parent_group = group_members(group_value, "935095190")
    assert screen_social_link({"platform": "facebook", "url": "https://facebook.com/fjellglodhudogvelvaere"}, legal_name="FJELLGLØD AS", group=parent_group).startswith("profile belongs to group company FJELLGLØD HUD OG VELVÆRE AS")


def test_exact_company_handles_from_earlier_runs_stay_published():
    assert screen_social_link({"url": "https://facebook.com/DekkpartnerNorge"}, legal_name="DEKK PARTNER AS") is None
    assert screen_social_link({"url": "https://facebook.com/nyttigno"}, legal_name="NYTT & NYTTIG AS") is None
    assert screen_social_link({"url": "https://facebook.com/novooi.official"}, legal_name="NOVOOI AS") is None
    assert screen_social_link({"url": "https://linkedin.com/company/ekeberg-sikkerhet-automasjon-as"}, legal_name="EKEBERG SIKKERHET OG AUTOMASJON AS") is None
    assert screen_social_link({"url": "https://instagram.com/spinno8"}, legal_name="SPINNO AS") is None


def _site_profile(org: str, name: str, url: str, socials: list[str], *, orgnr_proof: bool) -> dict:
    return {
        "organisation_number": org,
        "name": name,
        "evidence": {
            "website": {
                "status": "available",
                "source_url": url,
                "value": {
                    "final_url": url,
                    "identity_assessment": {"status": "exact", "publishable": True, "score": 1.0 if orgnr_proof else 0.95, "reasons": ["exact organisation number appears in homepage identity evidence" if orgnr_proof else "all normalized legal-name tokens appear together in homepage identity evidence"]},
                    "social_links": [{"platform": "facebook", "url": item} for item in socials],
                },
            },
            "public_activity": {"status": "available", "value": {"items": [{"title": "x", "url": url + "a", "published": "2026-01-01"}]}},
        },
    }


def test_a_profile_or_site_published_for_two_companies_is_withheld():
    a = _site_profile("111111111", "KJEDE OSLO AS", "https://kjede.no/", ["https://facebook.com/kjede"], orgnr_proof=False)
    b = _site_profile("222222222", "KJEDE BERGEN AS", "https://kjede.no/", ["https://facebook.com/kjede"], orgnr_proof=True)
    stats = apply_batch_consistency([a, b])
    assert stats["social_links_withheld"] == 2
    assert a["evidence"]["website"]["value"]["social_links"] == [] and b["evidence"]["website"]["value"]["social_links"] == []
    assert "linked from 2 companies" in a["evidence"]["website"]["value"]["social_withheld"][0]["reason"]
    # The site stays only where this company's own org number is printed on it.
    assert a["evidence"]["website"]["value"]["identity_assessment"]["publishable"] is False
    assert a["evidence"]["public_activity"]["status"] == "not_found"
    assert b["evidence"]["website"]["value"]["identity_assessment"]["publishable"] is True


# ------------------------------------------------------------------ confidence


def _profile_with_site(orgnr_proof: bool, news_withheld: list[dict] | None = None) -> dict:
    profile = _site_profile("935013623", "EKSEMPEL AS", "https://eksempel.no/", ["https://facebook.com/eksempel"], orgnr_proof=orgnr_proof)
    profile["legal_form"] = "AS"
    profile["evidence"]["registry_live"] = {"status": "available", "source_url": "https://data.brreg.no/x", "retrieved_at": "2026-11-02T00:00:00Z", "value": {"name": "EKSEMPEL AS", "legal_form": "AS"}}
    if news_withheld is not None:
        profile["evidence"]["public_activity"] = {"status": "not_found", "note": "Left blank: page has no date", "value": {"withheld": news_withheld}}
    profile["evidence"]["hiring"] = {"status": "not_found", "note": "Left blank: login-only applicant-system dashboard", "value": {"withheld": [{"url": "https://app.teamtailor.com/x", "reason": "login-only applicant-system dashboard"}]}}
    return profile


def test_org_number_proof_gives_high_confidence_and_name_only_medium():
    _, _, high = build_claims(_profile_with_site(orgnr_proof=True))
    _, _, medium = build_claims(_profile_with_site(orgnr_proof=False))
    assert high["confidence"]["level"] == "high"
    assert medium["confidence"]["level"] == "medium"
    claims, _, _ = build_claims(_profile_with_site(orgnr_proof=True))
    published = [item for item in claims if item["availability"] == "available"]
    assert published and all(isinstance(item["confidence"], float) for item in published)


def test_blank_news_and_jobs_carry_a_one_line_reason_and_no_value():
    profile = _profile_with_site(orgnr_proof=True, news_withheld=[{"title": "Hello world!", "url": "https://eksempel.no/hello-world/", "reason": "default CMS or static-page title, not news"}])
    claims, _, context = build_claims(profile)
    withheld = [item for item in claims if item["availability"] == "ambiguous"]
    assert {item["field"] for item in withheld} == {"public_activity.withheld", "hiring.withheld"}
    assert all(item["value"] is None and item["reason"] for item in withheld)
    sections = context["confidence"]["sections"]
    assert sections["news"]["state"] == "withheld" and sections["news"]["reason"] == "default CMS or static-page title, not news"
    assert sections["jobs"]["reason"] == "login-only applicant-system dashboard"
    summary = build_summary(profile, context)
    assert summary["confidence"] == "high"
    assert any("Left blank on purpose" in line for line in summary["unknowns"])


def test_viewer_shows_confidence_and_reasons():
    from scripts.build_prototype import build, compact

    profile = _profile_with_site(orgnr_proof=True)
    profile.update({"legal_form": "AS", "employees": None, "municipality": "OSLO", "industry_code": None, "industry_label": None, "website": None, "bankrupt": False, "liquidating": False})
    _, _, context = build_claims(profile)
    profile["confidence"] = context["confidence"]
    row = compact(profile)
    assert row["confidence"]["level"] == "high"
    assert row["siteJobs"]["withheld"][0]["reason"] == "login-only applicant-system dashboard"
    html = build([profile], None)
    assert "Match confidence" in html and "Any confidence" in html and "Left blank" in html


def test_article_page_must_print_its_own_publication_date():
    from norway_company_agent.quality import article_page_date

    wordpress_post = '<html><head><meta property="og:type" content="article"><meta property="article:published_time" content="2026-10-30T21:05:49+00:00"></head><body></body></html>'
    assert article_page_date(wordpress_post) == ("2026-10-30", None)
    blog_posting = '<script type="application/ld+json">{"@type":"BlogPosting","datePublished":"2025-01-01T12:52:06Z"}</script>'
    assert article_page_date(blog_posting) == ("2025-01-01", None)
    # A CMS stamps WebPage.datePublished on static pages too; that is not an article date.
    static_page = '<html><head><meta property="og:type" content="website"><script type="application/ld+json">{"@type":"WebPage","datePublished":"2023-04-01"}</script></head><body><h1>Om oss</h1></body></html>'
    assert article_page_date(static_page) == (None, "static page, not an article (page has no date)")
    assert article_page_date("<html><body><h1>Tjenester</h1></body></html>") == (None, "page has no date")
    assert article_page_date("<article><h2>Ny avtale</h2><time datetime='2026-09-01'>1. sep</time></article>") == ("2026-09-01", None)


def test_industry_descriptor_in_handle_is_not_another_entity():
    link = {"platform": "facebook", "url": "https://facebook.com/hitittreningssenter"}
    assert screen_social_link(link, legal_name="HIT IT AS", industry="Treningssentervirksomhet") is None
    assert screen_social_link(link, legal_name="HIT IT AS", industry="Regnskapsføring") == "handle carries extra words ('treningssenter') beyond the legal name"
    # A parent's brand page stays withheld whatever the industry says.
    assert screen_social_link({"url": "https://facebook.com/bremnesseashore"}, legal_name="SJØR AS", industry="Engroshandel med fisk, skalldyr og bløtdyr") is not None
