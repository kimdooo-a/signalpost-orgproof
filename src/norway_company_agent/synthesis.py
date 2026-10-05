"""Claim-level output and a deterministic, source-bounded company summary.

The summary is a template over published claims only. It never states a fact
that is not backed by an evidence id, and it lists what remains unknown
instead of guessing. No LLM is involved.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

SYNTHESIS_VERSION = "signalpost-template-summary-v1"

STATE_MAP = {
    "available": "available",
    "not_found": "not_available",
    "not_applicable": "not_applicable",
    "blocked": "blocked",
    "source_error": "failed",
    "not_fetched": "failed",
}


def _evidence_id(org: str, field: str, record: dict[str, Any]) -> str:
    basis = json.dumps([org, field, record.get("source_url"), record.get("content_sha256"), record.get("retrieved_at")], ensure_ascii=False)
    return "ev-" + hashlib.sha256(basis.encode()).hexdigest()[:16]


def _fmt_nok(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    if abs(value) >= 1_000_000:
        return f"NOK {value / 1_000_000:.1f}m"
    return f"NOK {value:,.0f}".replace(",", " ")


def build_claims(profile: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    org = profile["organisation_number"]
    records = profile.get("evidence", {})
    evidence_rows: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    ids: dict[str, str] = {}
    for field, record in records.items():
        if not isinstance(record, dict):
            continue
        ev_id = _evidence_id(org, field, record)
        ids[field] = ev_id
        evidence_rows.append({
            "id": ev_id,
            "field": field,
            "source_url": record.get("source_url"),
            "source_class": record.get("source_class") or record.get("source_type"),
            "retrieved_at": record.get("retrieved_at"),
            "reporting_period": record.get("as_of") or record.get("effective_at"),
            "content_sha256": record.get("content_sha256"),
            "extraction_method": record.get("source_type"),
            "availability": STATE_MAP.get(record.get("status"), "failed"),
            "note": record.get("note"),
        })

    def claim(field: str, value: Any, source: str, *, availability: str | None = None, period: str | None = None) -> None:
        record = records.get(source) or {}
        state = availability or STATE_MAP.get(record.get("status"), "failed")
        claims.append({
            "field": field,
            "value": value if state == "available" else None,
            "availability": state,
            "reporting_period": period,
            "evidence_ids": [ids[source]] if source in ids else [],
        })

    registry = (records.get("registry_live") or {}).get("value") or {}
    bulk = profile
    claim("legal_name", registry.get("name") or bulk.get("name"), "registry_live" if registry else "registry")
    claim("legal_form", registry.get("legal_form") or bulk.get("legal_form"), "registry_live" if registry else "registry")
    industry = registry.get("industry") or {}
    claim("industry", {"code": industry.get("kode") or bulk.get("industry_code"), "label": industry.get("beskrivelse") or bulk.get("industry_label")}, "registry_live" if registry else "registry")
    claim("business_address", registry.get("business_address"), "registry_live", availability=None if registry.get("business_address") else ("not_available" if registry else None))
    employees = registry.get("employees")
    claim("registered_employees", employees, "registry_live", availability=None if employees is not None else ("not_available" if registry else None))

    fin = (records.get("financials") or {}).get("value") or {}
    # Regnskapsregisteret returns periods oldest-first; the latest filing is the one with the max end date.
    fin_records = sorted(fin.get("records") or [], key=lambda item: str((item.get("period") or {}).get("tilDato") or ""), reverse=True)
    latest = fin_records[0] if fin_records else None
    if latest:
        period = (latest.get("period") or {}).get("tilDato")
        for key in ("revenue", "operating_result", "annual_result", "assets", "equity"):
            value = latest.get(key)
            claim(f"accounts.{key}", value, "financials", availability=None if value is not None else "not_available", period=period)
        for older in fin_records[1:]:
            older_period = (older.get("period") or {}).get("tilDato")
            for key in ("revenue", "annual_result"):
                value = older.get(key)
                claim(f"accounts_history.{key}", value, "financials", availability=None if value is not None else "not_available", period=older_period)
    else:
        claim("accounts.latest", None, "financials")

    roles = [item for item in ((records.get("roles") or {}).get("value") or {}).get("roles", []) if not item.get("inactive")]
    if roles:
        for item in roles[:20]:
            claim("leadership_role", {"name": item.get("name"), "role": item.get("role"), "last_changed": item.get("last_changed")}, "roles")
    else:
        claim("leadership_role", None, "roles", availability="not_available" if (records.get("roles") or {}).get("status") == "available" else None)

    locations = ((records.get("locations") or {}).get("value") or {}).get("locations", [])
    if locations:
        for item in locations[:20]:
            claim("registered_workplace", {"name": item.get("name"), "organisation_number": item.get("organisation_number"), "address": item.get("address")}, "locations")
    else:
        claim("registered_workplace", None, "locations", availability="not_available" if (records.get("locations") or {}).get("status") == "available" else None)

    website = records.get("website") or {}
    wvalue = website.get("value") or {}
    publishable = bool((wvalue.get("identity_assessment") or {}).get("publishable"))
    if website.get("status") == "available" and publishable:
        claim("official_website", wvalue.get("final_url"), "website")
        for link in wvalue.get("social_links") or []:
            claim(f"company_profile.{link['platform']}", link["url"], "website")
    elif website.get("status") == "available":
        claim("official_website", None, "website", availability="ambiguous")
    else:
        claim("official_website", None, "website")

    hiring = records.get("hiring") or {}
    hvalue = hiring.get("value") or {}
    if hiring.get("status") == "available":
        if hvalue.get("careers_page"):
            claim("hiring.careers_page", hvalue["careers_page"]["url"], "hiring")
        for job in hvalue.get("job_postings") or []:
            claim("hiring.job_posting", job, "hiring", period=job.get("posted"))
        for link in hvalue.get("applicant_tracking_links") or []:
            claim("hiring.ats_link", link, "hiring")
    else:
        claim("hiring", None, "hiring")

    activity = records.get("public_activity") or {}
    if activity.get("status") == "available":
        for item in (activity.get("value") or {}).get("items") or []:
            claim("public_activity.item", item, "public_activity", period=item.get("published"))
    else:
        claim("public_activity", None, "public_activity")

    context = {
        "registry": registry,
        "latest_accounts": latest,
        "accounts_history": fin_records[1:],
        "roles": roles,
        "locations": locations,
        "website": wvalue.get("final_url") if publishable else None,
        "website_discovered": website.get("source_type") == "discovered_company_website" and publishable,
        "socials": [link["platform"] for link in (wvalue.get("social_links") or [])] if publishable else [],
        "hiring": hvalue if hiring.get("status") == "available" else None,
        "activity": (activity.get("value") or {}).get("items") if activity.get("status") == "available" else None,
        "ids": ids,
        "description": (wvalue.get("description") or "")[:300] if publishable else "",
    }
    return claims, evidence_rows, context


def build_summary(profile: dict[str, Any], context: dict[str, Any], changes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    ids = context["ids"]
    registry = context["registry"]
    name = registry.get("name") or profile.get("name")
    industry = (registry.get("industry") or {}).get("beskrivelse") or profile.get("industry_label")
    address = registry.get("business_address") or {}
    place = address.get("poststed") or address.get("kommune") or profile.get("municipality")
    sentences: list[dict[str, Any]] = []
    unknowns: list[str] = []

    form = registry.get("legal_form") or profile.get("legal_form")
    first = f"{name} is registered in Enhetsregisteret" + (f" with legal form {form}" if form else "")
    if industry:
        first += f" registered under the industry '{industry}'"
    if place:
        first += f", with its business address in {str(place).title()}"
    sentences.append({"text": first + ".", "evidence_ids": [ids.get("registry_live") or ids.get("registry")]})
    if context.get("description"):
        sentences.append({"text": f"Its own website describes it as: \"{context['description']}\"", "evidence_ids": [ids.get("website")]})

    latest = context.get("latest_accounts")
    if latest:
        period = (latest.get("period") or {}).get("tilDato")
        parts = []
        for key, label in (("revenue", "revenue"), ("operating_result", "operating result"), ("annual_result", "net result"), ("equity", "equity")):
            text = _fmt_nok(latest.get(key))
            if text:
                parts.append(f"{label} {text}")
        if parts:
            sentences.append({"text": f"Latest filed annual accounts (period ending {period}): " + ", ".join(parts) + ".", "evidence_ids": [ids.get("financials")]})
        history = context.get("accounts_history") or []
        if history and _fmt_nok(latest.get("revenue")) and _fmt_nok(history[0].get("revenue")):
            prev_period = (history[0].get("period") or {}).get("tilDato")
            sentences.append({"text": f"Revenue moved from {_fmt_nok(history[0].get('revenue'))} (period ending {prev_period}) to {_fmt_nok(latest.get('revenue'))}.", "evidence_ids": [ids.get("financials")]})
    else:
        unknowns.append("Filed annual-account figures could not be retrieved from Regnskapsregisteret in this run.")

    roles = context.get("roles") or []
    leaders = [item for item in roles if item.get("role_code") in {"DAGL", "LEDE", "INNH"}]
    if leaders:
        sentences.append({"text": "Registered leadership: " + "; ".join(f"{item['name']} ({item['role']})" for item in leaders[:4]) + ".", "evidence_ids": [ids.get("roles")]})
    elif roles:
        sentences.append({"text": f"{len(roles)} active registered roles; no general manager or chair role is listed.", "evidence_ids": [ids.get("roles")]})
    else:
        unknowns.append("No active registered roles were returned.")

    locations = context.get("locations") or []
    if locations:
        sentences.append({"text": f"{len(locations)} registered workplace(s) (sub-units) in the official register.", "evidence_ids": [ids.get("locations")]})

    if context.get("website"):
        how = "found by a legal-name domain check and confirmed by the organisation number printed on the site" if context.get("website_discovered") else "listed in the official register and confirmed against the site content"
        text = f"Official website {context['website']} ({how})"
        if context.get("socials"):
            text += "; the site links company profiles on " + ", ".join(sorted(set(context["socials"])))
        sentences.append({"text": text + ".", "evidence_ids": [ids.get("website")]})
    else:
        unknowns.append("No website could be tied to this exact legal entity; none is published.")

    hiring = context.get("hiring")
    if hiring:
        jobs = len(hiring.get("job_postings") or [])
        ats = len(hiring.get("applicant_tracking_links") or [])
        bits = []
        if hiring.get("careers_page"):
            bits.append("a careers page")
        if jobs:
            bits.append(f"{jobs} structured job posting(s)")
        if ats:
            bits.append(f"{ats} link(s) to applicant-tracking systems")
        sentences.append({"text": "Hiring signals on the company site: " + ", ".join(bits) + ".", "evidence_ids": [ids.get("hiring")]})
    elif context.get("website"):
        unknowns.append("The verified website shows no careers page or job postings.")

    activity = context.get("activity")
    if activity:
        latest_item = activity[0]
        sentences.append({"text": f"Most recent dated activity on the company site: '{latest_item['title']}' ({latest_item['published']}); {len(activity)} dated item(s) found.", "evidence_ids": [ids.get("public_activity")]})
    elif context.get("website"):
        unknowns.append("No dated news or feed entries were found on the verified website.")

    if changes:
        sentences.append({"text": f"{len(changes)} material change(s) detected since the previous run.", "evidence_ids": []})
    else:
        sentences.append({"text": "No previous snapshot was supplied for comparison in this run, or nothing material changed.", "evidence_ids": []})

    for sentence in sentences:
        sentence["evidence_ids"] = [item for item in sentence["evidence_ids"] if item]
    return {
        "method": SYNTHESIS_VERSION,
        "policy": "Deterministic template over published claims only; no free-form AI text.",
        "sentences": sentences,
        "unknowns": unknowns,
    }
