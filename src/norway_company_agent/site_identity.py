"""Strict website-to-company tie (reviewer feedback, 13 November 2026).

The official run published sites that belonged to someone else: a restaurant
with the same name in Illinois (``isabellacafe.com`` for ISABELLA CAFE AS,
Ringerike) and a domain-for-sale page (``abho.com`` for ABHO EIENDOM AS).
Reviewer rule: a claim that cannot be tied to the company's name or
organisation number is dropped.

A website is published only when the fetched site itself ties it to this
registry entry:

* the exact organisation number is printed on the homepage or a same-domain
  contact/about page (spaces, dots, ``NO … MVA`` variants accepted), or
* the legal name's distinctive tokens appear together with the registry
  address (post code + post town, or street address) printed on the site, or
* for a registry-listed ``.no`` site only: the legal name plus a Norwegian
  signal (the ``.no`` domain itself; +47 / registry phone / registry
  municipality are recorded as supporting signals).

Non-Norwegian domains (``.com`` and the rest) need the organisation number or
the registry address. Guessed domains (derived from the name, so a name match
is close to tautological) need the organisation number or the registry
address on any TLD. Parked and for-sale pages are never published, and a site
that prints a different organisation number as its own is someone else's.

Everything here is pure (no network) and unit-tested.
"""
from __future__ import annotations

import re
import unicodedata
import urllib.parse
from typing import Any

import tldextract

SITE_IDENTITY_VERSION = "signalpost-site-identity-v3"

LEGAL_AND_GENERIC = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen", "stiftelse",
    "nuf", "ab", "b", "v", "limited", "ltd", "inc", "plc", "the", "og", "and", "i", "pa", "av",
}
# Tokens that describe a trade rather than name a company; they do not count as the distinctive part.
DESCRIPTIVE_TOKENS = {
    "eiendom", "holding", "invest", "investering", "drift", "service", "services", "consult", "consulting",
    "regnskap", "bygg", "transport", "handel", "utvikling", "norge", "norway", "norsk", "nordic", "group",
    "gruppen", "cafe", "kafe", "restaurant", "frisor", "salong", "eiendomsselskap", "utleie", "maskin",
    "entreprenor", "entreprenorer", "elektro", "ror", "vvs", "tjenester", "partner", "partners", "media",
}
ORGNR_CONTEXT = re.compile(r"\b(?:org(?:anisasjons|anisation|anization)?\.?\s*(?:nr|nummer|number|no)?\.?|foretaksregisteret|mva|vat|no)\s*[:.]?\s*$", re.IGNORECASE)


def norm(value: Any) -> str:
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def name_tokens(name: Any) -> list[str]:
    return [token for token in norm(name).split() if token not in LEGAL_AND_GENERIC and len(token) > 1]


def distinctive_tokens(name: Any) -> list[str]:
    return [token for token in name_tokens(name) if token not in DESCRIPTIVE_TOKENS and len(token) > 2]


def valid_orgnr(digits: str) -> bool:
    """Norwegian organisation numbers carry a mod-11 check digit."""
    if not re.fullmatch(r"[89]\d{8}", digits):
        return False
    weights = (3, 2, 7, 6, 5, 4, 3, 2)
    remainder = sum(int(d) * w for d, w in zip(digits[:8], weights)) % 11
    check = 0 if remainder == 0 else 11 - remainder
    return check != 10 and check == int(digits[8])


def orgnr_regex(org: str) -> re.Pattern[str]:
    digits = re.sub(r"\D", "", org)
    sep = r"[\s .\-]?"
    body = digits[0:3] + sep + digits[3:6] + sep + digits[6:9]
    return re.compile(r"(?<!\d)" + body + r"(?!\d)")


def printed_orgnrs(text: str) -> list[tuple[str, bool]]:
    """Valid organisation numbers printed in ``text`` and whether an "org.nr"/"MVA" label precedes them."""
    found: list[tuple[str, bool]] = []
    for match in re.finditer(r"(?<!\d)(\d{3})[\s .]?(\d{3})[\s .]?(\d{3})(?!\d)", text):
        digits = "".join(match.groups())
        if not valid_orgnr(digits):
            continue
        labelled = bool(ORGNR_CONTEXT.search(text[max(0, match.start() - 40) : match.start()]))
        found.append((digits, labelled))
    return found


def _registry_row(profile: dict[str, Any]) -> dict[str, Any]:
    return ((profile.get("evidence") or {}).get("registry") or {}).get("value") or {}


def registry_contact(profile: dict[str, Any]) -> dict[str, Any]:
    """Addresses, municipality and phone numbers from the registry row (bulk CSV keys or API keys)."""
    row = _registry_row(profile)
    live = (((profile.get("evidence") or {}).get("registry_live") or {}).get("value") or {})
    addresses: list[dict[str, str]] = []
    for prefix in ("forretningsadresse", "postadresse"):
        street = row.get(f"{prefix}.adresse")
        postcode = row.get(f"{prefix}.postnummer")
        town = row.get(f"{prefix}.poststed")
        municipality = row.get(f"{prefix}.kommune")
        if isinstance(row.get(prefix), dict):
            nested = row[prefix]
            street = street or ", ".join(nested.get("adresse") or [])
            postcode = postcode or nested.get("postnummer")
            town = town or nested.get("poststed")
            municipality = municipality or nested.get("kommune")
        if postcode or street:
            addresses.append({"street": str(street or ""), "postcode": str(postcode or ""), "town": str(town or ""), "municipality": str(municipality or "")})
    business = live.get("business_address") if isinstance(live, dict) else None
    if isinstance(business, dict):
        addresses.append({
            "street": ", ".join(business.get("adresse") or []) if isinstance(business.get("adresse"), list) else str(business.get("adresse") or ""),
            "postcode": str(business.get("postnummer") or ""),
            "town": str(business.get("poststed") or ""),
            "municipality": str(business.get("kommune") or ""),
        })
    phones = []
    for key in ("telefon", "mobil"):
        digits = re.sub(r"\D", "", str(row.get(key) or ""))
        if digits.startswith("47") and len(digits) == 10:
            digits = digits[2:]
        if len(digits) == 8:
            phones.append(digits)
    return {"addresses": addresses, "phones": sorted(set(phones))}


def address_on_page(addresses: list[dict[str, str]], text_norm: str) -> str | None:
    """Return a short description of the registry address found on the page, or None."""
    for address in addresses:
        postcode, town = address.get("postcode", ""), norm(address.get("town"))
        if re.fullmatch(r"\d{4}", postcode or "") and town:
            if re.search(rf"(?<!\d){postcode}\s+{re.escape(town)}\b", text_norm) or re.search(rf"\b{re.escape(town)}\s+{postcode}(?!\d)", text_norm):
                return f"registry post code and town ({postcode} {address.get('town')})"
        street = norm(address.get("street"))
        # A street address needs a house number to be distinctive ("Stabells gate 13", not "Storgata").
        if street and re.search(r"\d", street) and len(street) >= 8 and re.search(rf"\b{re.escape(street)}\b", text_norm):
            return "registry street address"
    return None


def phone_on_page(phones: list[str], text: str) -> bool:
    compact = re.sub(r"[\s .\-()]", "", text)
    return any(re.search(rf"(?<!\d)(?:\+?47|0047)?{phone}(?!\d)", compact) for phone in phones)


def tld(url: str) -> str:
    host = urllib.parse.urlparse(url or "").hostname or ""
    return tldextract.extract(host).suffix.casefold()


def _site_texts(value: dict[str, Any]) -> tuple[list[str], str]:
    """(homepage identity strings, all text from homepage and same-domain sub-pages)."""
    homepage = [
        str(value.get("title") or ""), str(value.get("description") or ""),
        urllib.parse.urlparse(value.get("final_url") or "").hostname or "",
    ]
    for org in value.get("structured_organisations") or []:
        if isinstance(org, dict):
            homepage.extend(str(org.get(key) or "") for key in ("name", "legalName", "alternateName"))
    parts = [*homepage, str(value.get("site_text") or ""), str(value.get("main_text_excerpt") or "")]
    for page in value.get("pages") or []:
        parts.extend(str(page.get(key) or "") for key in ("title", "site_text", "main_text_excerpt"))
    return homepage, "\n".join(parts)


def verify_site(profile: dict[str, Any], value: dict[str, Any], *, guessed: bool) -> dict[str, Any]:
    """Decide whether a fetched site is this company's own. Returns an assessment dict."""
    org = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    url = str(value.get("final_url") or "")
    homepage_parts, text = _site_texts(value)
    text_norm = norm(text)
    signals: dict[str, Any] = {"tld": tld(url), "guessed_domain": guessed}

    def result(publishable: bool, score: float, reason: str, method: str) -> dict[str, Any]:
        return {
            "status": "exact" if publishable else "related_or_uncertain",
            "score": score,
            "publishable": publishable,
            "reasons": [reason],
            "signals": signals,
            "method": method,
            "version": SITE_IDENTITY_VERSION,
        }

    if value.get("parked_reason"):
        signals["parked"] = True
        return result(False, 0.05, str(value["parked_reason"]), "parked_page_v1")

    own = bool(org) and bool(orgnr_regex(org).search(text))
    printed = printed_orgnrs(text)
    others = {digits for digits, _ in printed if digits != org}
    labelled_others = {digits for digits, labelled in printed if labelled and digits != org}
    signals.update({"orgnr": own, "other_orgnrs": len(others)})
    if own and len(others) >= 2:
        return result(False, 0.3, "page prints several other organisation numbers (directory, accountant or group page)", "orgnr_directory_v1")
    if own:
        match = orgnr_regex(org).search(text)
        signals["claim_span"] = " ".join(text[max(0, match.start() - 80) : match.end() + 40].split())
        return result(True, 1.0, "exact organisation number printed on the company site", "orgnr_on_site_v3")
    if labelled_others:
        return result(False, 0.2, "site prints a different organisation number as its own", "other_orgnr_v1")

    core = name_tokens(profile.get("name"))
    distinctive = distinctive_tokens(profile.get("name")) or core
    homepage_sets = [set(norm(part).split()) for part in homepage_parts if part]
    hostname_compact = "".join(norm(urllib.parse.urlparse(url).hostname or "").split())
    name_in_homepage = bool(core) and any(set(core) <= tokens for tokens in homepage_sets)
    name_in_host = bool(distinctive) and all(token in hostname_compact for token in distinctive)
    name_in_text = bool(core) and bool(re.search(r"\b" + r"\s+".join(map(re.escape, core)) + r"\b", text_norm))
    name = name_in_homepage or (name_in_host and name_in_text)
    contact = registry_contact(profile)
    address = address_on_page(contact["addresses"], text_norm)
    phone = phone_on_page(contact["phones"], text)
    municipalities = {norm(item.get("municipality")) for item in contact["addresses"] if item.get("municipality")}
    signals.update({
        "name": name,
        "registry_address": address,
        "registry_phone": phone,
        "plus47": bool(re.search(r"\+47[\s ]?\d", text)),
        "registry_municipality": any(m and re.search(rf"\b{re.escape(m)}\b", text_norm) for m in municipalities),
    })
    if not name:
        return result(False, 0.3, "site does not carry the legal name or the organisation number", "name_missing_v1")
    if address:
        return result(True, 0.95, f"legal name and {address} printed on the site", "name_and_registry_address_v1")
    if signals["tld"] != "no":
        return result(False, 0.4, f"non-Norwegian domain (.{signals['tld'] or '?'}) without the organisation number or registry address", "foreign_tld_v1")
    if guessed:
        return result(False, 0.5, "domain guessed from the name; name alone does not tie it to this company", "guessed_name_only_v1")
    return result(True, 0.9, "registry-listed .no site carries the legal name", "registry_no_domain_name_v1")
