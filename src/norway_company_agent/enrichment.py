"""Deterministic, zero-cost enrichment layered on top of the reference agent.

Three additions, all free and all company-owned or official sources:

1. Website discovery for companies whose registry row has no usable website.
   Candidate domains are derived from the legal name (``<name>.no`` and a few
   spelling variants). DNS resolution filters dead candidates without an HTTP
   request. A guessed domain is published only when the exact 9-digit
   organisation number is printed on the fetched homepage or on a same-domain
   contact/about page. Name similarity alone is never enough for a guessed
   domain, because a guessed domain has no registry link backing it.

2. Hiring signals from the verified site: careers pages, schema.org
   ``JobPosting`` objects and outbound links to recognised Norwegian
   applicant-tracking systems.

3. Dated public activity from the verified site: RSS/Atom feeds advertised by
   the site and dated news entries (``<time datetime>`` / JSON-LD
   ``datePublished``).

Every published item keeps source URL, retrieval time, content hash and the
extraction method. Nothing here calls an LLM or a paid API.
"""
from __future__ import annotations

import hashlib
import re
import socket
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

import extruct
from bs4 import BeautifulSoup

from .evidence import evidence, utc_now
from .identity import apply_website_identity_gate
from .website import (
    SAFE_OPENER,
    USER_AGENT,
    _registered_domain,
    _robots_allowed,
    assert_public_url,
    fetch_website,
)

ENRICHMENT_VERSION = "signalpost-enrichment-v1"

LEGAL_SUFFIXES = {
    "as", "asa", "ans", "da", "enk", "sa", "nuf", "ba", "bl", "iks", "ks", "sf", "stiftelse",
    "stiftelsen", "holding", "invest", "eiendom",
}
# Tokens that make a guessed domain too generic to be distinctive on its own.
GENERIC_TOKENS = {
    "norge", "norway", "norsk", "norske", "nordic", "group", "gruppen", "service", "services",
    "bygg", "transport", "consult", "consulting", "regnskap", "eiendom", "invest", "holding",
    "handel", "drift", "utvikling", "og", "and", "the", "i", "pa", "av",
}
CAREER_TERMS = (
    "karriere", "ledige-stillinger", "ledige_stillinger", "ledigestillinger", "stillinger", "jobb", "jobbe",
    "career", "careers", "jobs", "join-us", "work-with-us", "rekruttering",
)
NEWS_TERMS = ("nyheter", "aktuelt", "news", "presse", "press", "blogg", "blog", "artikler")
ATS_HOSTS = {
    "webcruiter.no": "webcruiter",
    "webcruiter.com": "webcruiter",
    "jobbnorge.no": "jobbnorge",
    "teamtailor.com": "teamtailor",
    "recman.no": "recman",
    "recman.page": "recman",
    "easycruit.com": "easycruit",
    "hrmanager.no": "hr-manager",
    "hr-manager.net": "hr-manager",
    "finn.no": "finn",
    "arbeidsplassen.nav.no": "nav_arbeidsplassen",
    "varbi.com": "varbi",
    "jobylon.com": "jobylon",
    "reachmee.com": "reachmee",
    "workday.com": "workday",
    "myworkdayjobs.com": "workday",
    "smartrecruiters.com": "smartrecruiters",
    "lever.co": "lever",
    "greenhouse.io": "greenhouse",
}
MAX_GUESSES = 4
MAX_EXTRA_PAGES = 3


def _ascii_tokens(value: Any) -> list[str]:
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    text = text.replace("&", " og ")
    return re.findall(r"[a-z0-9]+", text)


def candidate_domains(name: str | None) -> list[str]:
    """Deterministic domain guesses from a legal name. Order is stable."""
    tokens = [token for token in _ascii_tokens(name) if token not in LEGAL_SUFFIXES]
    if not tokens:
        return []
    distinctive = [token for token in tokens if token not in GENERIC_TOKENS and len(token) > 2]
    if not distinctive:
        return []
    joined = "".join(tokens)
    hyphen = "-".join(tokens)
    # Too short to be distinctive (e.g. "ab.no") or too long to be a real brand domain.
    guesses: list[str] = []
    if 4 <= len(joined) <= 40:
        guesses.append(f"{joined}.no")
    if len(tokens) > 1 and len(hyphen) <= 40:
        guesses.append(f"{hyphen}.no")
    without_og = [token for token in tokens if token not in {"og", "and"}]
    if len(without_og) != len(tokens) and 4 <= len("".join(without_og)) <= 40:
        guesses.append(f"{''.join(without_og)}.no")
    if 4 <= len(joined) <= 40:
        guesses.append(f"{joined}.com")
    # Norwegian letter variants: "ø" -> "o" is already applied; "oe"/"aa" spellings are common.
    raw = str(name or "").casefold()
    if any(ch in raw for ch in "øåæ"):
        alt = raw.translate(str.maketrans({"ø": "oe", "å": "aa", "æ": "ae"}))
        alt_tokens = [token for token in _ascii_tokens(alt) if token not in LEGAL_SUFFIXES]
        alt_joined = "".join(alt_tokens)
        if 4 <= len(alt_joined) <= 40:
            guesses.append(f"{alt_joined}.no")
    seen: list[str] = []
    for guess in guesses:
        if guess not in seen:
            seen.append(guess)
    return seen[:MAX_GUESSES]


def _resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return True
    except OSError:
        return False


def orgnr_pattern(org: str) -> re.Pattern[str]:
    digits = re.sub(r"\D", "", org)
    # 123456789, 123 456 789, 123.456.789, NO 123 456 789 MVA. Non-breaking spaces included.
    sep = r"[\s .\-]?"
    body = digits[0] + digits[1] + digits[2] + sep + digits[3] + digits[4] + digits[5] + sep + digits[6] + digits[7] + digits[8]
    return re.compile(r"(?<!\d)" + body + r"(?!\d)")


def _visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return soup.get_text(" ", strip=True)


def _distinct_orgnrs(text: str) -> set[str]:
    found = set()
    for match in re.finditer(r"(?<!\d)(\d{3})[\s .]?(\d{3})[\s .]?(\d{3})(?!\d)", text):
        found.add("".join(match.groups()))
    return found


def _get(url: str, *, timeout: float = 8.0, max_bytes: int = 1_500_000) -> dict[str, Any]:
    assert_public_url(url)
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/rss+xml,application/atom+xml,application/xml;q=0.9,*/*;q=0.5"})
    with SAFE_OPENER.open(request, timeout=timeout) as response:
        raw = response.read(max_bytes + 1)
        final_url = response.geturl()
        content_type = response.headers.get("content-type", "")
    assert_public_url(final_url)
    return {
        "url": final_url,
        "content_type": content_type,
        "raw": raw[:max_bytes],
        "sha256": hashlib.sha256(raw[:max_bytes]).hexdigest(),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "retrieved_at": utc_now(),
    }


def _proof_span(text: str, pattern: re.Pattern[str]) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    start = max(0, match.start() - 80)
    return text[start : match.end() + 40].strip()


def discover_website(profile: dict[str, Any], metrics: dict[str, Any], registry_url: str | None = None) -> dict[str, Any] | None:
    """Return an exact-identity website evidence record for a guessed domain, or None."""
    org = str(profile.get("organisation_number") or "")
    pattern = orgnr_pattern(org)
    attempts: list[dict[str, Any]] = []
    registry_domain = _registered_domain(registry_url) if registry_url else None
    domains = ([registry_domain] if registry_domain else []) + [item for item in candidate_domains(profile.get("name")) if item != registry_domain]
    for domain in domains:
        host_ok = _resolves(domain) or _resolves("www." + domain)
        attempt: dict[str, Any] = {"domain": domain, "dns": host_ok}
        attempts.append(attempt)
        if not host_ok:
            continue
        page = None
        for url in (f"https://{domain}/", f"https://www.{domain}/", f"http://www.{domain}/"):
            try:
                if not _robots_allowed(url, 6.0):
                    metrics["requests"] += 1
                    attempt["result"] = "robots_disallow"
                    break
                metrics["requests"] += 2
                page = _get(url)
                break
            except Exception as exc:  # network, TLS, unresolvable apex: try the next spelling
                attempt["result"] = f"fetch_error: {type(exc).__name__}"
                continue
        if page is None:
            continue
        metrics["bytes"] += len(page["raw"])
        metrics["latencies_ms"].append(page["elapsed_ms"])
        if "html" not in page["content_type"].lower():
            attempt["result"] = "not_html"
            continue
        html = page["raw"].decode("utf-8", errors="replace")
        text = _visible_text(html)
        proof_url, proof_text, proof_hash = page["url"], text, page["sha256"]
        if not pattern.search(text):
            # Look one hop deeper: a same-domain contact/about page often carries the org number.
            soup = BeautifulSoup(html, "lxml")
            contact = None
            for anchor in soup.select("a[href]"):
                href = urllib.parse.urljoin(page["url"], str(anchor.get("href") or ""))
                path = (urllib.parse.urlparse(href).path + " " + anchor.get_text(" ", strip=True)).casefold()
                if _registered_domain(href) == _registered_domain(page["url"]) and any(term in path for term in ("kontakt", "contact", "om-oss", "om oss", "about")):
                    contact = href
                    break
            if contact:
                try:
                    metrics["requests"] += 1
                    sub = _get(contact)
                    metrics["bytes"] += len(sub["raw"])
                    sub_text = _visible_text(sub["raw"].decode("utf-8", errors="replace"))
                    if pattern.search(sub_text) and _registered_domain(sub["url"]) == _registered_domain(page["url"]):
                        proof_url, proof_text, proof_hash = sub["url"], sub_text, sub["sha256"]
                except Exception:
                    pass
        span = _proof_span(proof_text, pattern)
        if not span:
            attempt["result"] = "no_orgnr_proof"
            continue
        # A page that prints many different organisation numbers is a directory, portfolio or
        # accountant client list, not the company's own site.
        if len(_distinct_orgnrs(proof_text) - {org}) >= 2:
            attempt["result"] = "multiple_orgnrs_directory_like"
            continue
        attempt["result"] = "exact_orgnr_proof"
        record, site_metrics = fetch_website(page["url"])
        metrics["requests"] += site_metrics["requests"]
        metrics["bytes"] += site_metrics["bytes"]
        metrics["latencies_ms"].extend(site_metrics["latencies_ms"])
        if record.get("status") != "available":
            attempt["result"] = "refetch_failed"
            continue
        record["source_type"] = "discovered_company_website"
        record["source_class"] = "company_owned_discovered"
        gated = apply_website_identity_gate(profile, record)["website"]
        value = gated.get("value") or {}
        # The org-number proof is stronger than the name heuristic; record it explicitly.
        value["identity_assessment"] = {
            "status": "exact",
            "score": 1.0,
            "publishable": True,
            "reasons": ["exact organisation number printed on the company site"],
            "method": "orgnr_proof_on_guessed_domain_v1",
            "proof": {"url": proof_url, "content_sha256": proof_hash, "claim_span": span},
        }
        # The name heuristic may have quarantined the site's social links; with org-number proof the
        # site is exact, so re-apply only the per-link handle check.
        value["social_links"] = [
            {"platform": item["platform"], "url": item["url"]}
            for item in value.get("social_link_assessments") or []
            if item.get("publishable")
        ]
        from_registry = domain == registry_domain
        value["discovery"] = {"method": "registry_url_orgnr_proof_v1" if from_registry else "legal_name_domain_guess_v1", "attempts": attempts}
        if from_registry:
            record["source_type"] = "registry_linked_company_website"
            record["source_class"] = "registry_linked_company_website"
        gated["value"] = value
        gated["note"] = "Website discovered by legal-name domain guess; published only after exact org-number proof on the site"
        return gated
    profile.setdefault("enrichment_trace", {})["website_discovery"] = attempts
    return None


def _same_site_links(base_url: str, soup: BeautifulSoup, terms: tuple[str, ...], limit: int) -> list[str]:
    base_domain = _registered_domain(base_url)
    ranked: dict[str, int] = {}
    for anchor in soup.select("a[href]"):
        href = urllib.parse.urljoin(base_url, str(anchor.get("href") or "").strip())
        parsed = urllib.parse.urlparse(href)
        if parsed.scheme not in {"http", "https"} or _registered_domain(href) != base_domain:
            continue
        haystack = (parsed.path + " " + anchor.get_text(" ", strip=True)).casefold()
        rank = next((index for index, term in enumerate(terms) if term in haystack), None)
        if rank is None:
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if clean.rstrip("/") == base_url.rstrip("/"):
            continue
        ranked[clean] = min(rank, ranked.get(clean, rank))
    return [url for url, _ in sorted(ranked.items(), key=lambda item: (item[1], item[0]))[:limit]]


def _ats_links(base_url: str, soup: BeautifulSoup) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for anchor in soup.select("a[href]"):
        href = urllib.parse.urljoin(base_url, str(anchor.get("href") or "").strip())
        host = (urllib.parse.urlparse(href).hostname or "").casefold().removeprefix("www.")
        platform = next((label for domain, label in ATS_HOSTS.items() if host == domain or host.endswith("." + domain)), None)
        if not platform:
            continue
        # finn.no / nav links are only hiring evidence when they point at a job ad.
        if platform == "finn" and "/job" not in href:
            continue
        if platform == "nav_arbeidsplassen" and "/stillinger/" not in href:
            continue
        found[href] = {"platform": platform, "url": href, "anchor_text": anchor.get_text(" ", strip=True)[:200]}
    return sorted(found.values(), key=lambda item: item["url"])


def _structured(html: str, url: str) -> list[Any]:
    try:
        data = extruct.extract(html, base_url=url, syntaxes=["json-ld", "microdata"])
    except Exception:
        return []
    return list(data.get("json-ld") or []) + list(data.get("microdata") or [])


def _walk_types(value: Any, wanted: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(value, dict):
        kind = value.get("@type") or value.get("type")
        kinds = set(kind if isinstance(kind, list) else [kind])
        kinds = {str(item).rsplit("/", 1)[-1] for item in kinds if item}
        if kinds & wanted:
            out.append(value)
        for child in value.values():
            out.extend(_walk_types(child, wanted))
    elif isinstance(value, list):
        for child in value:
            out.extend(_walk_types(child, wanted))
    return out


def _iso_date(value: Any) -> str | None:
    text = str(value or "").strip()
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return "-".join(match.groups())
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_feed(raw: bytes, feed_url: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    items: list[dict[str, Any]] = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in {"item", "entry"}:
            continue
        fields = {child.tag.rsplit("}", 1)[-1]: child for child in node}
        title = (fields.get("title").text or "").strip() if fields.get("title") is not None else ""
        link_node = fields.get("link")
        link = ""
        if link_node is not None:
            link = (link_node.text or link_node.get("href") or "").strip()
        date_node = next((fields[key] for key in ("pubDate", "published", "updated", "date") if fields.get(key) is not None), None)  # Element truthiness is False when it has no children
        date = _iso_date(date_node.text if date_node is not None else None)
        if title and date:
            items.append({"title": title[:300], "url": urllib.parse.urljoin(feed_url, link) if link else feed_url, "published": date})
    return items[:20]


def _page_dated_items(html: str, url: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for obj in _walk_types(_structured(html, url), {"NewsArticle", "Article", "BlogPosting", "PressRelease"}):
        date = _iso_date(obj.get("datePublished") or obj.get("dateCreated"))
        title = str(obj.get("headline") or obj.get("name") or "").strip()
        if date and title:
            items.append({"title": title[:300], "url": str(obj.get("url") or url), "published": date, "method": "schema_org_article"})
    soup = BeautifulSoup(html, "lxml")
    for node in soup.select("article"):
        time_node = node.select_one("time[datetime]")
        heading = node.select_one("h1, h2, h3, h4")
        link = node.select_one("a[href]")
        if not time_node or not heading:
            continue
        date = _iso_date(time_node.get("datetime"))
        if date:
            items.append({
                "title": heading.get_text(" ", strip=True)[:300],
                "url": urllib.parse.urljoin(url, str(link.get("href"))) if link else url,
                "published": date,
                "method": "html_article_time",
            })
    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        dedup[(item["title"], item["published"])] = item
    return sorted(dedup.values(), key=lambda item: item["published"], reverse=True)[:20]


def enrich_from_site(profile: dict[str, Any], website: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    """Hiring and dated-activity evidence from a verified company-owned site."""
    value = website.get("value") or {}
    assessment = value.get("identity_assessment") or {}
    base_url = value.get("final_url") or website.get("source_url")
    if website.get("status") != "available" or not assessment.get("publishable") or not base_url:
        reason = "no exact-identity company website to read from"
        return {
            "hiring": evidence("hiring", "not_found", "company_owned_site", base_url or "", note=reason),
            "public_activity": evidence("public_activity", "not_found", "company_owned_site", base_url or "", note=reason),
        }
    try:
        home = _get(base_url)
        metrics["requests"] += 1
        metrics["bytes"] += len(home["raw"])
        metrics["latencies_ms"].append(home["elapsed_ms"])
    except Exception as exc:
        note = f"homepage re-fetch failed: {type(exc).__name__}"
        return {
            "hiring": evidence("hiring", "source_error", "company_owned_site", base_url, note=note),
            "public_activity": evidence("public_activity", "source_error", "company_owned_site", base_url, note=note),
        }
    html = home["raw"].decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    pages_read = [{"url": home["url"], "content_sha256": home["sha256"], "retrieved_at": home["retrieved_at"]}]

    careers = _same_site_links(home["url"], soup, CAREER_TERMS, 1)
    news = _same_site_links(home["url"], soup, NEWS_TERMS, 1)
    feeds = []
    for link in soup.select('link[rel="alternate"][href]'):
        kind = str(link.get("type") or "").casefold()
        if "rss" in kind or "atom" in kind:
            feed = urllib.parse.urljoin(home["url"], str(link.get("href")))
            if _registered_domain(feed) == _registered_domain(home["url"]):
                feeds.append(feed)
    jobs: list[dict[str, Any]] = []
    ats = _ats_links(home["url"], soup)
    for obj in _walk_types(_structured(html, home["url"]), {"JobPosting"}):
        jobs.append({"title": str(obj.get("title") or "")[:300], "posted": _iso_date(obj.get("datePosted")), "valid_through": _iso_date(obj.get("validThrough")), "url": home["url"], "method": "schema_org_jobposting"})
    activity = _page_dated_items(html, home["url"])

    extra = 0
    career_page = None
    for page_url in careers + news + feeds[:1]:
        if extra >= MAX_EXTRA_PAGES:
            break
        try:
            if not _robots_allowed(page_url, 6.0):
                metrics["requests"] += 1
                continue
            page = _get(page_url)
            metrics["requests"] += 2
            metrics["bytes"] += len(page["raw"])
            metrics["latencies_ms"].append(page["elapsed_ms"])
        except Exception:
            continue
        extra += 1
        if _registered_domain(page["url"]) != _registered_domain(home["url"]):
            continue
        pages_read.append({"url": page["url"], "content_sha256": page["sha256"], "retrieved_at": page["retrieved_at"]})
        body = page["raw"].decode("utf-8", errors="replace")
        if page_url in feeds:
            for item in _parse_feed(page["raw"], page["url"]):
                item["method"] = "company_rss_feed"
                item["source_url"] = page["url"]
                activity.append(item)
            continue
        page_soup = BeautifulSoup(body, "lxml")
        if page_url in careers:
            career_page = {"url": page["url"], "title": page_soup.title.get_text(" ", strip=True)[:300] if page_soup.title else "", "content_sha256": page["sha256"], "retrieved_at": page["retrieved_at"]}
            ats.extend(_ats_links(page["url"], page_soup))
            for obj in _walk_types(_structured(body, page["url"]), {"JobPosting"}):
                jobs.append({"title": str(obj.get("title") or "")[:300], "posted": _iso_date(obj.get("datePosted")), "valid_through": _iso_date(obj.get("validThrough")), "url": page["url"], "method": "schema_org_jobposting"})
        if page_url in news:
            for item in _page_dated_items(body, page["url"]):
                item["source_url"] = page["url"]
                activity.append(item)

    ats = list({item["url"]: item for item in ats}.values())
    activity = list({(item["title"], item["published"]): item for item in activity}.values())
    activity.sort(key=lambda item: item["published"], reverse=True)
    retrieved = utc_now()
    if jobs or ats or career_page:
        hiring = evidence(
            "hiring",
            "available",
            "company_owned_site",
            (career_page or {}).get("url") or home["url"],
            value={
                "careers_page": career_page,
                "job_postings": jobs[:30],
                "applicant_tracking_links": ats[:30],
                "pages_read": pages_read,
                "extractor": ENRICHMENT_VERSION,
            },
            retrieved_at=retrieved,
            content_sha256=(career_page or {}).get("content_sha256") or home["sha256"],
            note="Hiring signals from the exact-identity company site. A careers page is not proof of an open vacancy; job_postings/ATS links are.",
        )
    else:
        hiring = evidence("hiring", "not_found", "company_owned_site", home["url"], value={"pages_read": pages_read, "extractor": ENRICHMENT_VERSION}, retrieved_at=retrieved, note="Checked the verified site; no careers page, job posting or ATS link found")
    if activity:
        public_activity = evidence(
            "public_activity",
            "available",
            "company_owned_site",
            activity[0].get("source_url") or home["url"],
            value={"items": activity[:20], "pages_read": pages_read, "extractor": ENRICHMENT_VERSION},
            retrieved_at=retrieved,
            effective_at=activity[0]["published"],
            note="Dated items published by the exact-identity company site",
        )
    else:
        public_activity = evidence("public_activity", "not_found", "company_owned_site", home["url"], value={"pages_read": pages_read, "extractor": ENRICHMENT_VERSION}, retrieved_at=retrieved, note="Checked the verified site; no dated news, article or feed entries found")
    return {"hiring": hiring, "public_activity": public_activity}
