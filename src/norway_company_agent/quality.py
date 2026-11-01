"""Publication gates for news, jobs and social matches, plus per-company confidence.

Reviewer feedback on the first official run (1 November 2026):

1. News leaked the most points: a default WordPress "Hello world!" post and a
   static page with no date were published as news.
2. Jobs: a login-only Teamtailor dashboard and the Teamtailor homepage were
   published as hiring evidence.
3. A group's Facebook page was published as the company's own page.

Rule of thumb from the same feedback: a wrong match is worse than a blank.
Every gate here therefore *withholds* rather than guesses, and every withheld
candidate keeps a one-line reason so the viewer can show "can't tell" instead
of an empty box.

All functions are pure (no network) so they are unit-tested directly.
"""
from __future__ import annotations

import json
import re
import unicodedata
import urllib.parse
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

import tldextract
from bs4 import BeautifulSoup

QUALITY_VERSION = "signalpost-publication-gates-v2"

# --------------------------------------------------------------------------- news

# Titles that CMSs create by default or that name a static page, compared after
# normalisation (case, accents and punctuation removed).
PLACEHOLDER_TITLES = {
    "hello world", "hei verden", "hallo verden", "sample page", "eksempelside", "example page",
    "privacy policy", "personvernerklaering", "personvern", "cookies", "cookie policy", "cookiepolicy",
    "uncategorized", "ukategorisert", "test", "testinnlegg", "test post", "testside", "draft", "utkast",
    "coming soon", "kommer snart", "under construction", "under arbeid", "home", "hjem", "forside",
    "front page", "kontakt", "kontakt oss", "contact", "contact us", "om oss", "about", "about us",
    "nyheter", "news", "aktuelt", "blogg", "blog", "arkiv", "archive", "artikler", "articles",
    "hello world welcome to wordpress", "welcome to wordpress",
}
PLACEHOLDER_PREFIXES = ("comment on ", "kommentar til ", "kommentarer til ", "comments on ")
# Path segments that mark a static page rather than an article.
STATIC_PATH_SEGMENTS = {
    "om-oss", "om_oss", "omoss", "about", "about-us", "kontakt", "kontakt-oss", "contact", "contact-us",
    "personvern", "personvernerklaering", "privacy", "privacy-policy", "cookies", "cookie-policy",
    "vilkar", "vilkaar", "terms", "tjenester", "services", "produkter", "products", "ansatte", "team",
    "sample-page", "eksempelside", "home", "hjem", "forside", "category", "kategori", "tag", "author",
    "forfatter", "karriere", "career", "careers", "jobb", "ledige-stillinger",
}
EARLIEST_PLAUSIBLE = date(1995, 1, 1)


def norm_text(value: Any) -> str:
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def registered_domain(url: str) -> str:
    host = urllib.parse.urlparse(url or "").hostname or ""
    return tldextract.extract(host).top_domain_under_public_suffix


def _clean_path(url: str) -> str:
    return (urllib.parse.urlparse(url or "").path or "/").rstrip("/") or "/"


def _today(today: date | None) -> date:
    return today or datetime.now(UTC).date()


def screen_news_item(item: dict[str, Any], *, site_url: str, read_from: Iterable[str] = (), today: date | None = None) -> str | None:
    """Return None when the item is a dated article about the company, otherwise a one-line reason."""
    published = str(item.get("published") or "").strip()
    if not published:
        return "page has no date"
    try:
        when = date.fromisoformat(published[:10])
    except ValueError:
        return "date could not be read"
    if when > _today(today):
        return "date is in the future"
    if when < EARLIEST_PLAUSIBLE:
        return "date is implausibly old"
    title = norm_text(item.get("title"))
    if not title:
        return "item has no title"
    if title in PLACEHOLDER_TITLES or any(title.startswith(prefix.strip()) for prefix in PLACEHOLDER_PREFIXES):
        return "default CMS or static-page title, not news"
    url = str(item.get("url") or "")
    if not url:
        return "item has no own URL"
    if site_url and registered_domain(url) != registered_domain(site_url):
        return "item links off the company's own site"
    path = _clean_path(url)
    if path == "/":
        return "item points at the homepage, not an article"
    listing_paths = {_clean_path(page) for page in read_from if page}
    if path in listing_paths:
        return "date belongs to a listing or static page, not an article"
    segments = [segment.casefold() for segment in path.split("/") if segment]
    if len(segments) == 1 and segments[0] in STATIC_PATH_SEGMENTS:
        return "static page, not a news article"
    if segments and segments[0] in {"category", "kategori", "tag", "author", "forfatter"}:
        return "archive page, not an article"
    if re.search(r"(?:^|/)(?:feed|comments)(?:/|$)", path):
        return "feed or comment URL, not an article"
    return None


def screen_news(items: list[dict[str, Any]], *, site_url: str, read_from: Iterable[str] = (), today: date | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    read_from = list(read_from)
    kept: list[dict[str, Any]] = []
    withheld: list[dict[str, Any]] = []
    for item in items:
        reason = screen_news_item(item, site_url=site_url, read_from=read_from, today=today)
        if reason:
            withheld.append({"title": str(item.get("title") or "")[:200], "url": item.get("url"), "published": item.get("published"), "reason": reason})
        else:
            kept.append(item)
    return kept, withheld


ARTICLE_TYPES = {"Article", "NewsArticle", "BlogPosting", "PressRelease", "Report", "AnalysisNewsArticle"}
DATE_META_NAMES = {"article:published_time", "date", "pubdate", "publishdate", "publish-date", "publish_date", "dc.date", "dc.date.issued", "dcterms.created", "sailthru.date", "datepublished"}


def article_page_date(html: str) -> tuple[str | None, str | None]:
    """Publication date printed by an article page itself, or the reason it has none.

    Only article-level signals count: ``article:published_time`` and similar
    meta tags, JSON-LD Article/BlogPosting/NewsArticle ``datePublished``, or a
    ``<time datetime>`` inside the article. A WebPage ``datePublished`` is not
    enough, because CMSs stamp that on static pages too.
    """
    soup = BeautifulSoup(html or "", "lxml")
    found: list[str] = []
    og_type = ""
    for meta in soup.select("meta[content]"):
        key = str(meta.get("property") or meta.get("name") or meta.get("itemprop") or "").strip().casefold()
        if key == "og:type":
            og_type = str(meta.get("content") or "").casefold()
        elif key in DATE_META_NAMES:
            found.append(str(meta.get("content") or ""))
    for node in soup.select('[itemprop="datePublished"]'):
        found.append(str(node.get("content") or node.get("datetime") or node.get_text(" ", strip=True)))

    def walk(value):
        if isinstance(value, dict):
            kind = value.get("@type")
            kinds = {str(item) for item in (kind if isinstance(kind, list) else [kind]) if item}
            if kinds & ARTICLE_TYPES and value.get("datePublished"):
                found.append(str(value["datePublished"]))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            walk(json.loads(script.string or script.get_text() or "null"))
        except (ValueError, TypeError):
            continue
    for node in soup.select("article time[datetime], main time[datetime], time[datetime][itemprop], time[datetime].published, time[datetime].entry-date"):
        found.append(str(node.get("datetime") or ""))
    for raw in found:
        match = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", raw)
        if match:
            return "-".join(match.groups()), None
    if og_type == "website":
        return None, "static page, not an article (page has no date)"
    return None, "page has no date"


def is_comment_feed(url: str, title: str = "") -> bool:
    return bool(re.search(r"comment|kommentar", f"{url} {title}", re.IGNORECASE))


# --------------------------------------------------------------------------- jobs

# Vendors whose employer boards live on a company sub-domain. The bare vendor
# host is the vendor's own marketing site.
SUBDOMAIN_BOARD_VENDORS = {"teamtailor.com", "recman.page", "jobylon.com", "varbi.com"}
VENDOR_SERVICE_SUBDOMAINS = {
    "www", "app", "apps", "admin", "login", "auth", "sso", "dashboard", "career", "careers", "support",
    "help", "status", "blog", "go", "partner", "partners", "marketplace", "docs", "api", "cdn", "assets",
    "integrations", "community", "academy", "info",
}
LOGIN_PATH = re.compile(
    r"(?:^|/)(?:login|log-in|logg-inn|logginn|signin|sign-in|sign_in|users/sign_in|session|sessions|sso|oauth|auth|"
    r"dashboard|admin|my|min-side|minside|account|konto)(?:/|$)",
    re.IGNORECASE,
)
POWERED_BY = re.compile(r"powered by|levert av|drevet av|laget med|made with|built with|rekrutteringssystem", re.IGNORECASE)


def classify_ats_link(url: str, anchor_text: str = "", platform: str = "") -> str | None:
    """Return None for an employer job board or posting, otherwise the reason it is withheld."""
    parsed = urllib.parse.urlparse(url or "")
    host = (parsed.hostname or "").casefold()
    path = parsed.path or "/"
    if POWERED_BY.search(anchor_text or ""):
        return "vendor 'powered by' link, not a job board"
    if LOGIN_PATH.search(path) or host.startswith(("app.", "admin.", "login.", "auth.", "sso.", "dashboard.")):
        return "login-only applicant-system dashboard"
    query = urllib.parse.parse_qs(parsed.query)
    if any(key.startswith("utm_") for key in query) and path.rstrip("/") in {"", "/"}:
        return "vendor homepage (tracking link), not a job board"
    domain = tldextract.extract(host).top_domain_under_public_suffix
    subdomain = host[: -len(domain)].rstrip(".") if domain and host.endswith(domain) else ""
    first_label = subdomain.split(".")[0] if subdomain else ""
    if domain in SUBDOMAIN_BOARD_VENDORS:
        if not first_label or first_label in VENDOR_SERVICE_SUBDOMAINS:
            return "applicant-system vendor homepage, not the company's job board"
        return None
    if domain in {"workday.com"}:
        return "applicant-system vendor homepage, not the company's job board"
    segments = [segment for segment in path.split("/") if segment]
    meaningful_query = {key for key in query if not key.startswith("utm_")}
    if not segments and not meaningful_query:
        return "applicant-system vendor homepage, not the company's job board"
    if (not first_label or first_label == "www") and len(segments) == 1 and segments[0].casefold() in {"om-oss", "about", "pricing", "priser", "kontakt", "contact", "features", "funksjoner", "blog", "blogg", "kunder", "customers"}:
        return "applicant-system vendor marketing page, not a job board"
    return None


def screen_ats_links(links: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    withheld: list[dict[str, Any]] = []
    for link in links:
        reason = classify_ats_link(str(link.get("url") or ""), str(link.get("anchor_text") or ""), str(link.get("platform") or ""))
        if reason:
            withheld.append({"url": link.get("url"), "platform": link.get("platform"), "reason": reason})
        else:
            kept.append(link)
    return kept, withheld


# Phrases that only a page about the company's own vacancies prints. "Rekruttering" is deliberately
# absent: on an agency or HR-consultancy site it names the service being sold, not the company's hiring.
CAREER_BODY_PHRASES = ("ledige stillinger", "ledig stilling", "sok jobb", "jobbe hos oss", "jobb hos oss", "bli en del av teamet", "apen soknad", "open positions", "open roles", "vacancies", "join our team", "work with us")


def screen_careers_page(page: dict[str, Any] | None, *, site_url: str, body_text: str = "", has_password_field: bool = False) -> str | None:
    """A careers page is published only when it is the company's own, reachable and about jobs."""
    if not page:
        return "no careers page"
    url = str(page.get("url") or "")
    if site_url and registered_domain(url) != registered_domain(site_url):
        return "careers link leaves the company's own site"
    path = _clean_path(url)
    if path == "/" or path == _clean_path(site_url):
        return "careers link redirects to the homepage"
    if has_password_field or LOGIN_PATH.search(path):
        return "login-only page, not a public careers page"
    haystack = norm_text(f"{path} {page.get('title') or ''}")
    if not any(term in haystack for term in ("karriere", "stilling", "jobb", "career", "jobs", "join", "work with")):
        body = norm_text(body_text[:20000])
        if not any(phrase in body for phrase in CAREER_BODY_PHRASES):
            return "page is not about this company's own vacancies"
    return None


def screen_job_posting(job: dict[str, Any], *, today: date | None = None) -> str | None:
    if not str(job.get("title") or "").strip():
        return "posting has no title"
    valid = job.get("valid_through")
    if valid:
        try:
            if date.fromisoformat(str(valid)[:10]) < _today(today):
                return f"posting closed on {str(valid)[:10]}"
        except ValueError:
            pass
    return None


# --------------------------------------------------------------------------- social

LEGAL_AND_GENERIC = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen",
    "nuf", "ab", "b", "v", "limited", "ltd", "inc", "plc", "the", "og", "and",
}
# Handle affixes that do not name a different entity.
BENIGN_AFFIXES = ("official", "offisiell", "offisielt", "norge", "norway", "norsk", "page", "side", "hq", "no", "as", "the", "real", "team")
GROUP_MARKERS = ("group", "gruppen", "gruppa", "konsern", "holding", "kjeden", "kjede", "alliance", "allianse", "forbund", "network", "nettverk")


def _name_tokens(value: Any) -> list[str]:
    return [token for token in norm_text(value).split() if token not in LEGAL_AND_GENERIC and len(token) > 1]


def _handle(url: str) -> str:
    path = urllib.parse.unquote(urllib.parse.urlparse(url or "").path)
    parts = [part for part in path.split("/") if part]
    if parts and parts[0].casefold() in {"company", "channel", "user", "c"}:
        parts = parts[1:]
    return "".join(norm_text(" ".join(parts)).split())


def _residual(handle: str, tokens: list[str]) -> str:
    rest = handle
    for token in sorted(set(tokens), key=len, reverse=True):
        rest = rest.replace(token, "", 1)
    rest = re.sub(r"\d+", "", rest)
    changed = True
    while changed and rest:
        changed = False
        for affix in BENIGN_AFFIXES:
            if rest.startswith(affix) or rest.endswith(affix):
                rest = rest[len(affix):] if rest.startswith(affix) else rest[: -len(affix)]
                changed = True
    return rest


def group_members(group_value: Any, own_org: str) -> list[dict[str, str]]:
    """Every other company in the official group structure (konsernstruktur)."""
    found: dict[str, str] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            org = str(node.get("organisasjonsnummer") or "")
            name = node.get("navn")
            if org and name and org != own_org:
                found[org] = str(name)
            parent_org = str(node.get("parentOrganisasjonsnummer") or "")
            if parent_org and node.get("parentNavn") and parent_org != own_org:
                found[parent_org] = str(node["parentNavn"])
            for child in node.get("children") or []:
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(group_value)
    return [{"organisation_number": org, "name": name} for org, name in sorted(found.items())]


def screen_social_link(link: dict[str, Any], *, legal_name: str, group: list[dict[str, str]] | None = None, industry: str | None = None) -> str | None:
    """Publish a social profile only when its handle names this exact legal entity.

    A group, parent, sister or brand page usually carries only part of the legal
    name (the shared brand) or names another company in the group. Both are
    withheld.
    """
    tokens = _name_tokens(legal_name)
    handle = _handle(str(link.get("url") or ""))
    if not tokens or not handle:
        return "profile handle cannot be compared with the legal name"
    own_complete = all(token in handle for token in tokens)
    for member in group or []:
        member_tokens = _name_tokens(member.get("name"))
        if not member_tokens or set(member_tokens) == set(tokens):
            continue
        if all(token in handle for token in member_tokens) and (not own_complete or set(member_tokens) > set(tokens)):
            return f"profile belongs to group company {member.get('name')} ({member.get('organisation_number')})"
    for marker in GROUP_MARKERS:
        if marker in handle and marker not in tokens:
            return f"handle names a group ('{marker}'), not this company"
    missing = [token for token in tokens if token not in handle]
    if missing:
        return "handle matches only part of the legal name; may be a group or brand page"
    rest = _residual(handle, tokens)
    # An extra word that describes the registered industry ("hitittreningssenter" for HIT IT AS,
    # industry "Treningssentervirksomhet") is a descriptor, not another entity.
    industry_compact = "".join(norm_text(industry).split())
    if len(rest) > 3 and not (len(rest) >= 5 and rest in industry_compact):
        return f"handle carries extra words ('{rest}') beyond the legal name"
    return None


def screen_social_links(links: list[dict[str, Any]], *, legal_name: str, group: list[dict[str, str]] | None = None, industry: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    withheld: list[dict[str, Any]] = []
    for link in links:
        reason = screen_social_link(link, legal_name=legal_name, group=group, industry=industry)
        if reason:
            withheld.append({"platform": link.get("platform"), "url": link.get("url"), "reason": reason})
        else:
            kept.append({"platform": link.get("platform"), "url": link.get("url")})
    return kept, withheld


def shared_across_companies(published: dict[str, list[str]]) -> dict[str, list[str]]:
    """Map url -> organisation numbers for urls that more than one company published."""
    owners: dict[str, set[str]] = {}
    for org, urls in published.items():
        for url in urls:
            owners.setdefault(url.rstrip("/").casefold(), set()).add(org)
    return {url: sorted(orgs) for url, orgs in owners.items() if len(orgs) > 1}


# --------------------------------------------------------------------------- confidence

REGISTRY_CONFIDENCE = 0.99
SITE_ORGNR_CONFIDENCE = 0.97
SITE_NAME_CONFIDENCE = 0.85


def site_confidence(website_value: dict[str, Any]) -> float | None:
    assessment = (website_value or {}).get("identity_assessment") or {}
    if not assessment.get("publishable"):
        return None
    reasons = " ".join(assessment.get("reasons") or []).casefold()
    if float(assessment.get("score") or 0) >= 1.0 or "organisation number" in reasons:
        return SITE_ORGNR_CONFIDENCE
    return SITE_NAME_CONFIDENCE


def level(value: float) -> str:
    return "high" if value >= 0.9 else "medium" if value >= 0.75 else "low"


def apply_batch_consistency(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    """Withhold matches that the same run published for more than one company.

    A social profile or website linked from several companies' sites is a group,
    chain or agency page; we cannot tell which legal entity owns it, so it is
    withheld for every company. A shared website stays published only for a
    company whose own organisation number is printed on it.
    """
    socials: dict[str, list[str]] = {}
    sites: dict[str, list[str]] = {}
    by_org = {str(profile.get("organisation_number")): profile for profile in profiles}
    for org, profile in by_org.items():
        website = profile.get("evidence", {}).get("website") or {}
        value = website.get("value") or {}
        if website.get("status") != "available" or not (value.get("identity_assessment") or {}).get("publishable"):
            continue
        socials[org] = [item["url"] for item in value.get("social_links") or []]
        if value.get("final_url"):
            sites[org] = [value["final_url"]]
    shared_socials = shared_across_companies(socials)
    shared_sites = shared_across_companies(sites)
    withheld_socials = 0
    withheld_sites = 0
    for url, orgs in shared_socials.items():
        for org in orgs:
            value = by_org[org]["evidence"]["website"]["value"]
            keep = [item for item in value.get("social_links") or [] if item["url"].rstrip("/").casefold() != url]
            if len(keep) != len(value.get("social_links") or []):
                withheld_socials += 1
                value.setdefault("social_withheld", []).append({"url": url, "reason": f"same profile is linked from {len(orgs)} companies in this run; cannot tell which one owns it"})
            value["social_links"] = keep
    for url, orgs in shared_sites.items():
        for org in orgs:
            website = by_org[org]["evidence"]["website"]
            value = website["value"]
            if site_confidence(value) == SITE_ORGNR_CONFIDENCE:
                continue
            others = [item for item in orgs if item != org]
            assessment = dict(value.get("identity_assessment") or {})
            assessment.update({"status": "related_or_uncertain", "publishable": False})
            assessment.setdefault("reasons", [])
            assessment["reasons"] = list(assessment["reasons"]) + [f"same website is published for {len(orgs)} companies ({', '.join(others[:3])}) and this company's org number is not on it"]
            value["identity_assessment"] = assessment
            value["withheld_reason"] = "website is shared with other companies in this run; org number not printed"
            value["social_links"] = []
            for key in ("hiring", "public_activity"):
                record = by_org[org]["evidence"].get(key)
                if record and record.get("status") == "available":
                    record["status"] = "not_found"
                    record["note"] = "Left blank: the website could not be tied to this exact company"
                    record.setdefault("value", {})["withheld_by_site"] = True
            withheld_sites += 1
    return {"shared_social_urls": len(shared_socials), "social_links_withheld": withheld_socials, "shared_websites": len(shared_sites), "websites_withheld": withheld_sites}
