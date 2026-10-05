from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.enrichment import (
    _distinct_orgnrs,
    _page_dated_items,
    _parse_feed,
    candidate_domains,
    orgnr_pattern,
)
from norway_company_agent.synthesis import build_claims, build_summary


def test_candidate_domains_are_deterministic_and_bounded():
    assert candidate_domains("Bergen Rør og Varme AS") == [
        "bergenrorogvarme.no",
        "bergen-ror-og-varme.no",
        "bergenrorvarme.no",
        "bergenrorogvarme.com",
    ]
    assert candidate_domains("A B AS") == []
    assert candidate_domains("Holding AS") == []
    assert len(candidate_domains("Ålesund Bilsenter AS")) <= 4


def test_orgnr_pattern_accepts_common_print_formats_only_for_exact_number():
    pattern = orgnr_pattern("935013623")
    assert pattern.search("minKrypto AS - 935 013 623 E-post")
    assert pattern.search("Org.nr: 935013623 MVA")
    assert pattern.search("NO 935.013.623 MVA")
    assert not pattern.search("Org.nr 9350136231")
    assert not pattern.search("Org.nr 935 013 624")


def test_directory_like_pages_expose_multiple_orgnrs():
    text = "Kunde A 912345678 Kunde B 923 456 789 Kunde C 934567890"
    assert len(_distinct_orgnrs(text)) == 3


def test_feed_items_with_childless_date_elements_are_parsed():
    raw = b"""<?xml version='1.0'?><rss version='2.0'><channel><title>x</title>
    <item><title>Showroom Salg</title><link>https://example.no/sale/</link>
    <pubDate>Mon, 28 Sep 2026 10:00:00 +0000</pubDate></item></channel></rss>"""
    items = _parse_feed(raw, "https://example.no/feed/")
    assert items == [{"title": "Showroom Salg", "url": "https://example.no/sale/", "published": "2026-09-28"}]


def test_dated_article_items_from_html():
    html = "<html><body><article><h2>Ny avtale</h2><time datetime='2026-09-01T08:00'>1. sep</time><a href='/nyheter/ny-avtale'>les</a></article></body></html>"
    items = _page_dated_items(html, "https://example.no/nyheter")
    assert items[0]["published"] == "2026-09-01"
    assert items[0]["url"] == "https://example.no/nyheter/ny-avtale"


def _profile() -> dict:
    return {
        "organisation_number": "935013623",
        "name": "EKSEMPEL AS",
        "legal_form": "AS",
        "evidence": {
            "registry_live": {"status": "available", "source_url": "https://data.brreg.no/x", "retrieved_at": "2026-10-05T00:00:00Z", "source_type": "official_registry_live", "value": {"name": "EKSEMPEL AS", "legal_form": "AS", "employees": None, "industry": {"kode": "71.110", "beskrivelse": "Arkitektvirksomhet"}, "business_address": {"poststed": "OSLO"}}},
            "financials": {"status": "available", "source_url": "https://data.brreg.no/r", "retrieved_at": "2026-10-05T00:00:00Z", "source_type": "official_annual_accounts", "value": {"records": [
                {"period": {"tilDato": "2023-12-31"}, "revenue": 100.0},
                {"period": {"tilDato": "2025-12-31"}, "revenue": 300.0, "annual_result": None},
                {"period": {"tilDato": "2024-12-31"}, "revenue": 200.0},
            ]}},
            "roles": {"status": "available", "source_url": "https://data.brreg.no/roller", "retrieved_at": "2026-10-05T00:00:00Z", "source_type": "official_roles", "value": {"roles": []}},
            "website": {"status": "not_found", "source_url": "", "retrieved_at": "2026-10-05T00:00:00Z", "source_type": "registry_linked_company_website"},
        },
    }


def test_claims_use_latest_period_and_never_turn_missing_into_zero():
    claims, evidence_rows, context = build_claims(_profile())
    revenue = next(item for item in claims if item["field"] == "accounts.revenue")
    assert revenue["value"] == 300.0 and revenue["reporting_period"] == "2025-12-31"
    result = next(item for item in claims if item["field"] == "accounts.annual_result")
    assert result["availability"] == "not_available" and result["value"] is None
    employees = next(item for item in claims if item["field"] == "registered_employees")
    assert employees["availability"] == "not_available" and employees["value"] is None
    website = next(item for item in claims if item["field"] == "official_website")
    assert website["availability"] == "not_available"
    assert all(row["id"].startswith("ev-") for row in evidence_rows)
    summary = build_summary(_profile(), context)
    assert any("No website could be tied" in item for item in summary["unknowns"])
    assert all(sentence["evidence_ids"] or "previous snapshot" in sentence["text"] or "change" in sentence["text"] for sentence in summary["sentences"])
