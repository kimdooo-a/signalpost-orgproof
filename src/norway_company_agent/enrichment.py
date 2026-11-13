"""Deterministic, zero-cost enrichment layered on top of the reference agent.

Three additions, all free and all company-owned or official sources:

1. Website discovery for companies whose registry row has no usable website.
   Candidate domains are derived from the legal name (``<name>.no`` and a few
   spelling variants). DNS resolution filters dead candidates without an HTTP
   request. A guessed domain is published only when the exact 9-digit
   organisation number, or the legal name with the registry address, is
   printed on the fetched homepage or a same-domain contact/about page
   (``site_identity.verify_site``). Name similarity alone is never enough for
   a guessed domain, because a guessed domain has no registry link backing it.

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
from .quality import (
    QUALITY_VERSION,
    article_page_date,
    careers_posting_links,
    group_members,
    is_comment_feed,
    listing_article_links,
    page_title,
    posting_from_page,
    screen_ats_links,
    screen_careers_page,
    screen_job_posting,
    screen_news,
    screen_social_links,
)
from .site_identity import verify_site
from .website import (
    SAFE_OPENER,
    USER_AGENT,
    _registered_domain,
    assert_public_url,
    fetch_website,
    robots_check,
)


def _own_url(candidate: Any, fallback: str) -> str:
    """A JobPosting's own ``url`` when it stays on the same site, otherwise the page it was read from."""
    text = str(candidate or "").strip()
    if text.startswith(("http://", "https://")) and _registered_domain(text) == _registered_domain(fallback):
        return text
    return fallback

ENRICHMENT_VERSION = "signalpost-enrichment-v2"

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
    "career", "careers", "jobs", "join-us", "work-with-us",
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
# Items read off a homepage or listing page are opened before publication; the
# article page itself must print its publication date. Feed entries carry their
# own pubDate and are not re-fetched.
MAX_ARTICLE_CHECKS = 6
# Postings linked from the careers page that are opened to read title and deadline.
MAX_POSTING_CHECKS = 3


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


def _ascii_url(url: str) -> str:
    """Percent-encode a link copied from a page ("/nyheter/ny-båt") so urllib can request it."""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    try:
        host_ascii = host.encode("idna").decode("ascii")
    except UnicodeError:
        host_ascii = host
    netloc = host_ascii + (f":{parsed.port}" if parsed.port else "")
    safe = "/:@!$&'()*+,;=%-._~"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, urllib.parse.quote(parsed.path, safe=safe), urllib.parse.quote(parsed.query, safe=safe + "?"), ""))


def _decode(page: dict[str, Any]) -> str:
    """Page text in its declared charset (HTTP header or <meta charset>), falling back to UTF-8 then cp1252."""
    raw = page.get("raw") or b""
    declared = re.search(r"charset=[\"']?([A-Za-z0-9_\-]+)", str(page.get("content_type") or ""))
    if not declared:
        declared = re.search(rb"<meta[^>]+charset=[\"']?([A-Za-z0-9_\-]+)", raw[:4096], re.IGNORECASE)
    names = [declared.group(1).decode() if isinstance(declared.group(1), bytes) else declared.group(1)] if declared else []
    for name in [*names, "utf-8"]:
        try:
            return raw.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("cp1252", errors="replace")


def _get(url: str, *, timeout: float = 8.0, max_bytes: int = 1_500_000) -> dict[str, Any]:
    url = _ascii_url(url)
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


def apply_strict_site_gate(profile: dict[str, Any], website: dict[str, Any], *, guessed: bool) -> dict[str, Any]:
    """Second, stricter website gate (reviewer feedback 2026-11-13).

    The name heuristic (``identity.assess_website_identity``) may only keep a
    site that ``site_identity.verify_site`` also ties to this registry entry.
    The organisation number printed on a same-domain sub-page is strong enough
    on its own. A rejected site loses its social links, and nothing derived
    from it is published.
    """
    if website.get("status") != "available":
        return website
    value = website.get("value") or {}
    name_assessment = value.get("identity_assessment") or {}
    strict = verify_site(profile, value, guessed=guessed)
    # The org number, or the legal name with the registry address, ties the site on its own; a name-only
    # match (registry-listed .no) must also pass the name heuristic.
    ties_alone = strict["method"].startswith(("orgnr", "name_and_registry_address"))
    if strict["publishable"] and (ties_alone or name_assessment.get("publishable")):
        final = strict
    else:
        reasons = list(strict.get("reasons") or [])
        if strict["publishable"]:
            reasons = list(name_assessment.get("reasons") or []) or ["site could not be tied to this exact company"]
        final = {**strict, "status": "related_or_uncertain", "publishable": False, "reasons": reasons}
    final["name_heuristic"] = {key: name_assessment.get(key) for key in ("status", "score", "reasons")}
    value["identity_assessment"] = final
    if not final["publishable"]:
        value["social_links"] = []
        value["withheld_reason"] = final["reasons"][-1] if final.get("reasons") else "site could not be tied to this exact company"
    website["value"] = value
    return website


def discover_website(profile: dict[str, Any], metrics: dict[str, Any], registry_url: str | None = None) -> dict[str, Any] | None:
    """Return an exact-identity website evidence record for a registry or guessed domain, or None.

    Each candidate is fetched once (homepage + up to two contact/about pages)
    and must pass ``site_identity.verify_site``: the organisation number on
    the site, or the legal name with the registry address. A guessed domain is
    never published on the name alone.
    """
    attempts: list[dict[str, Any]] = []
    registry_domain = _registered_domain(registry_url) if registry_url else None
    domains = ([registry_domain] if registry_domain else []) + [item for item in candidate_domains(profile.get("name")) if item != registry_domain]
    for domain in domains:
        host_ok = _resolves(domain) or _resolves("www." + domain)
        attempt: dict[str, Any] = {"domain": domain, "dns": host_ok}
        attempts.append(attempt)
        if not host_ok:
            continue
        record = None
        for url in (f"https://{domain}/", f"https://www.{domain}/", f"http://www.{domain}/"):
            record, site_metrics = fetch_website(url, timeout=8.0, max_pages=2)
            metrics["requests"] += site_metrics["requests"]
            metrics["bytes"] += site_metrics["bytes"]
            metrics["latencies_ms"].extend(site_metrics["latencies_ms"])
            if record.get("status") == "available" or record.get("status") == "blocked":
                break
        if record is None or record.get("status") != "available":
            attempt["result"] = "robots_disallow" if record and record.get("status") == "blocked" else "fetch_failed"
            continue
        final_domain = _registered_domain((record.get("value") or {}).get("final_url") or "")
        if final_domain != domain:
            # A brand's .no that forwards to its own .com is common; a guess that lands on a directory,
            # marketplace or another company is not. Follow only when the new domain still names the company.
            final_compact = re.sub(r"[^a-z0-9]", "", final_domain.rsplit(".", 1)[0])
            distinctive = [token for token in _ascii_tokens(profile.get("name")) if token not in LEGAL_SUFFIXES and token not in GENERIC_TOKENS and len(token) > 2]
            if not distinctive or not all(token in final_compact for token in distinctive):
                attempt["result"] = "redirects_to_another_domain"
                continue
            attempt["redirected"] = True
        from_registry = domain == registry_domain
        record["source_type"] = "registry_linked_company_website" if from_registry else "discovered_company_website"
        record["source_class"] = "registry_linked_company_website" if from_registry else "company_owned_discovered"
        gated = apply_website_identity_gate(profile, record)["website"]
        value = gated.get("value") or {}
        strict = verify_site(profile, value, guessed=not from_registry)
        attempt["result"] = strict["method"]
        if not strict["publishable"]:
            continue
        if strict["method"] == "registry_no_domain_name_v1" and not (value.get("identity_assessment") or {}).get("publishable"):
            attempt["result"] = "name_heuristic_rejected"
            continue
        strict["name_heuristic"] = {key: (value.get("identity_assessment") or {}).get(key) for key in ("status", "score", "reasons")}
        value["identity_assessment"] = strict
        # The name heuristic may have quarantined the site's social links; with the site tied to the
        # registry entry, re-apply only the per-link handle check.
        value["social_links"] = [
            {"platform": item["platform"], "url": item["url"]}
            for item in value.get("social_link_assessments") or []
            if item.get("publishable")
        ]
        value["discovery"] = {"method": "registry_url_strict_identity_v3" if from_registry else "legal_name_domain_guess_v3", "attempts": attempts}
        gated["value"] = value
        gated["note"] = "Website published only after the site itself tied it to this registry entry: " + strict["reasons"][0]
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
        # Terms must start a word or path segment: "stillinger" inside "/bestillinger" is a booking page.
        rank = next((index for index, term in enumerate(terms) if re.search(r"(?<![a-z0-9æøå])" + re.escape(term), haystack)), None)
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
        # Atom entries can carry several <link> elements (alternate, replies, edit); only the
        # alternate (or rel-less) link is the article itself.
        link_nodes = [child for child in node if child.tag.rsplit("}", 1)[-1] == "link"]
        link_node = next((child for child in link_nodes if (child.get("rel") or "alternate") == "alternate"), None)
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
    html = _decode(home)
    soup = BeautifulSoup(html, "lxml")
    pages_read = [{"url": home["url"], "content_sha256": home["sha256"], "retrieved_at": home["retrieved_at"]}]

    careers = _same_site_links(home["url"], soup, CAREER_TERMS, 1)
    news = _same_site_links(home["url"], soup, NEWS_TERMS, 1)
    feeds = []
    for link in soup.select('link[rel="alternate"][href]'):
        kind = str(link.get("type") or "").casefold()
        if "rss" in kind or "atom" in kind:
            feed = urllib.parse.urljoin(home["url"], str(link.get("href")))
            # WordPress advertises a comments feed next to the posts feed; comments are not news.
            if is_comment_feed(feed, str(link.get("title") or "")):
                continue
            if _registered_domain(feed) == _registered_domain(home["url"]):
                feeds.append(feed)
    jobs: list[dict[str, Any]] = []
    ats = _ats_links(home["url"], soup)
    for obj in _walk_types(_structured(html, home["url"]), {"JobPosting"}):
        jobs.append({"title": str(obj.get("title") or "")[:300], "posted": _iso_date(obj.get("datePosted")), "valid_through": _iso_date(obj.get("validThrough")), "url": home["url"], "method": "schema_org_jobposting"})
    activity = _page_dated_items(html, home["url"])

    extra = 0
    career_page = None
    career_check: dict[str, Any] = {}
    news_listing_pages: list[str] = []
    posting_links: list[dict[str, str]] = []
    posting_withheld: list[dict[str, Any]] = []
    for page_url in careers + news + feeds[:1]:
        if extra >= MAX_EXTRA_PAGES:
            break
        try:
            allowed, robots_requests = robots_check(page_url, 6.0)
            metrics["requests"] += robots_requests
            if not allowed:
                continue
            page = _get(page_url)
            metrics["requests"] += 1
            metrics["bytes"] += len(page["raw"])
            metrics["latencies_ms"].append(page["elapsed_ms"])
        except Exception:
            continue
        extra += 1
        if _registered_domain(page["url"]) != _registered_domain(home["url"]):
            continue
        pages_read.append({"url": page["url"], "content_sha256": page["sha256"], "retrieved_at": page["retrieved_at"]})
        body = _decode(page)
        if page_url in feeds:
            for item in _parse_feed(page["raw"], page["url"]):
                item["method"] = "company_rss_feed"
                item["source_url"] = page["url"]
                activity.append(item)
            continue
        page_soup = BeautifulSoup(body, "lxml")
        if page_url in careers:
            career_page = {"url": page["url"], "title": page_soup.title.get_text(" ", strip=True)[:300] if page_soup.title else "", "content_sha256": page["sha256"], "retrieved_at": page["retrieved_at"]}
            career_check = {"body_text": _visible_text(body), "has_password_field": bool(page_soup.select('input[type="password"]'))}
            ats.extend(_ats_links(page["url"], page_soup))
            for obj in _walk_types(_structured(body, page["url"]), {"JobPosting"}):
                jobs.append({"title": str(obj.get("title") or "")[:300], "posted": _iso_date(obj.get("datePosted")), "valid_through": _iso_date(obj.get("validThrough")), "url": _own_url(obj.get("url"), page["url"]), "method": "schema_org_jobposting"})
            posting_links.extend(careers_posting_links(page["url"], body, site_url=home["url"]))
        if page_url in news:
            news_listing_pages.append(page["url"])
            for item in _page_dated_items(body, page["url"]):
                item["source_url"] = page["url"]
                activity.append(item)
            # Links to individual articles; each must print its own date when opened.
            for item in listing_article_links(page["url"], body, site_url=home["url"], limit=MAX_ARTICLE_CHECKS):
                item["source_url"] = page["url"]
                activity.append(item)

    # Individual postings linked from the careers page: published only with a title and an open deadline
    # (or a recent posting date) printed on the posting page itself.
    posting_checks = 0
    for link in posting_links:
        if posting_checks >= MAX_POSTING_CHECKS or any(job.get("url") == link["url"] for job in jobs):
            continue
        posting_checks += 1
        try:
            allowed, robots_requests = robots_check(link["url"], 6.0)
            metrics["requests"] += robots_requests
            if not allowed:
                posting_withheld.append({"url": link["url"], "kind": "job_posting", "reason": "posting page disallowed by robots.txt"})
                continue
            page = _get(link["url"])
            metrics["requests"] += 1
            metrics["bytes"] += len(page["raw"])
            metrics["latencies_ms"].append(page["elapsed_ms"])
        except Exception as exc:
            posting_withheld.append({"url": link["url"], "kind": "job_posting", "reason": f"posting page could not be opened ({type(exc).__name__})"})
            continue
        if _registered_domain(page["url"]) != _registered_domain(home["url"]):
            posting_withheld.append({"url": link["url"], "kind": "job_posting", "reason": "posting redirects off the company's own site"})
            continue
        job, reason = posting_from_page(_decode(page), page["url"])
        if job is None:
            posting_withheld.append({"url": link["url"], "kind": "job_posting", "reason": reason})
            continue
        job["content_sha256"] = page["sha256"]
        job["retrieved_at"] = page["retrieved_at"]
        jobs.append(job)
        pages_read.append({"url": page["url"], "content_sha256": page["sha256"], "retrieved_at": page["retrieved_at"]})

    ats = list({item["url"]: item for item in ats}.values())
    dated_urls = {item.get("url") for item in activity if item.get("published")}
    deduped: dict[Any, dict[str, Any]] = {}
    for item in activity:
        if not item.get("published") and item.get("url") in dated_urls:
            continue
        key = (item["title"], item["published"]) if item.get("published") else ("url", item.get("url"))
        deduped.setdefault(key, item)
    activity = list(deduped.values())
    # Dated items first (newest first), then undated listing links in page order.
    activity.sort(key=lambda item: (bool(item.get("published")), item.get("published") or ""), reverse=True)
    # Publication gates (reviewer feedback 2026-11-01): a wrong match is worse than a blank.
    activity, news_withheld = screen_news(activity, site_url=home["url"], read_from=[home["url"], *news_listing_pages])
    activity, verify_withheld = verify_article_pages(activity, home["url"], metrics)
    news_withheld += verify_withheld
    # Re-screen with the dates and headlines the article pages printed themselves.
    activity, rescreen_withheld = screen_news(activity, site_url=home["url"], read_from=[home["url"], *news_listing_pages])
    news_withheld += rescreen_withheld
    ats, ats_withheld = screen_ats_links(ats)
    jobs_withheld: list[dict[str, Any]] = list(posting_withheld)
    kept_jobs: list[dict[str, Any]] = []
    for job in jobs:
        reason = screen_job_posting(job)
        if reason:
            jobs_withheld.append({"title": job.get("title"), "url": job.get("url"), "reason": reason})
        else:
            kept_jobs.append(job)
    jobs = kept_jobs
    if career_page:
        reason = screen_careers_page(career_page, site_url=home["url"], body_text=career_check.get("body_text", ""), has_password_field=career_check.get("has_password_field", False))
        if reason:
            jobs_withheld.append({"url": career_page["url"], "kind": "careers_page", "reason": reason})
            career_page = None
    hiring_withheld = ats_withheld + jobs_withheld
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
                "withheld": hiring_withheld[:30],
                "pages_read": pages_read,
                "extractor": ENRICHMENT_VERSION,
                "gates": QUALITY_VERSION,
            },
            retrieved_at=retrieved,
            content_sha256=(career_page or {}).get("content_sha256") or home["sha256"],
            note="Hiring signals from the exact-identity company site. A careers page is not proof of an open vacancy; job_postings/ATS links are.",
        )
    else:
        note = "Checked the verified site; no careers page, job posting or ATS link found"
        if hiring_withheld:
            note = f"Left blank: {hiring_withheld[0]['reason']}" + (f" (+{len(hiring_withheld) - 1} more withheld)" if len(hiring_withheld) > 1 else "")
        hiring = evidence("hiring", "not_found", "company_owned_site", home["url"], value={"withheld": hiring_withheld[:30], "pages_read": pages_read, "extractor": ENRICHMENT_VERSION, "gates": QUALITY_VERSION}, retrieved_at=retrieved, note=note)
    if activity:
        public_activity = evidence(
            "public_activity",
            "available",
            "company_owned_site",
            activity[0].get("source_url") or home["url"],
            value={"items": activity[:20], "withheld": news_withheld[:20], "pages_read": pages_read, "extractor": ENRICHMENT_VERSION, "gates": QUALITY_VERSION},
            retrieved_at=retrieved,
            effective_at=activity[0]["published"],
            note="Dated items published by the exact-identity company site",
        )
    else:
        note = "Checked the verified site; no dated news, article or feed entries found"
        if news_withheld:
            note = f"Left blank: {news_withheld[0]['reason']}" + (f" (+{len(news_withheld) - 1} more withheld)" if len(news_withheld) > 1 else "")
        public_activity = evidence("public_activity", "not_found", "company_owned_site", home["url"], value={"withheld": news_withheld[:20], "pages_read": pages_read, "extractor": ENRICHMENT_VERSION, "gates": QUALITY_VERSION}, retrieved_at=retrieved, note=note)
    return {"hiring": hiring, "public_activity": public_activity}


def gate_social_links(profile: dict[str, Any]) -> dict[str, Any]:
    """Withhold social profiles that may belong to a group, parent, sister or brand.

    Runs after the website identity gate. The handle must name this exact legal
    entity; a handle that names another company in the official group structure
    (BRREG konsernstruktur) is withheld with that company named in the reason.
    """
    website = profile.get("evidence", {}).get("website") or {}
    value = website.get("value") or {}
    assessment = value.get("identity_assessment") or {}
    if website.get("status") != "available" or not assessment.get("publishable"):
        return website
    group_record = profile.get("evidence", {}).get("group") or {}
    group = group_members(group_record.get("value"), str(profile.get("organisation_number") or "")) if group_record.get("status") == "available" else []
    kept, withheld = screen_social_links(list(value.get("social_links") or []), legal_name=str(profile.get("name") or ""), group=group, industry=profile.get("industry_label"))
    published = {item["url"] for item in kept}
    for item in value.get("social_link_assessments") or []:
        if not item.get("publishable") and item.get("url") not in published and not any(w.get("url") == item.get("url") for w in withheld):
            withheld.append({"platform": item.get("platform"), "url": item.get("url"), "reason": "handle does not carry the legal name"})
    value["social_links"] = kept
    value["social_withheld"] = withheld
    value["group_members_checked"] = len(group)
    website["value"] = value
    return website


def verify_article_pages(items: list[dict[str, Any]], site_url: str, metrics: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Open items that did not come from a feed and keep them only if the page prints a date."""
    kept: list[dict[str, Any]] = []
    withheld: list[dict[str, Any]] = []
    checks = 0
    for item in items:
        if item.get("method") == "company_rss_feed":
            kept.append(item)
            continue
        if checks >= MAX_ARTICLE_CHECKS:
            withheld.append({"title": item.get("title"), "url": item.get("url"), "published": item.get("published"), "reason": "article page not opened within the request budget"})
            continue
        checks += 1
        try:
            allowed, robots_requests = robots_check(item["url"], 6.0)
            metrics["requests"] += robots_requests
            if not allowed:
                withheld.append({"title": item.get("title"), "url": item.get("url"), "published": item.get("published"), "reason": "article page disallowed by robots.txt"})
                continue
            page = _get(item["url"])
            metrics["requests"] += 1
            metrics["bytes"] += len(page["raw"])
            metrics["latencies_ms"].append(page["elapsed_ms"])
        except Exception as exc:
            withheld.append({"title": item.get("title"), "url": item.get("url"), "published": item.get("published"), "reason": f"article page could not be opened ({type(exc).__name__})"})
            continue
        if _registered_domain(page["url"]) != _registered_domain(site_url):
            withheld.append({"title": item.get("title"), "url": item.get("url"), "published": item.get("published"), "reason": "article redirects off the company's own site"})
            continue
        page_html = _decode(page)
        page_date, reason = article_page_date(page_html)
        if not page_date:
            withheld.append({"title": item.get("title"), "url": item.get("url"), "published": item.get("published"), "reason": reason})
            continue
        title = item.get("title")
        if item.get("method") == "listing_link":
            # The article's own headline beats the anchor text it was linked with.
            title = page_title(page_html) or title
        kept.append({**item, "title": title, "published": page_date, "page_verified": True, "content_sha256": page["sha256"], "url": page["url"]})
    kept.sort(key=lambda item: item["published"], reverse=True)
    return kept, withheld
