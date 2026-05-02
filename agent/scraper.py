"""
Scrapers for WSV deal-flow digest.

  1. CourtListener  — PACER bankruptcy dockets (public HTML search)
  2. SEC EDGAR      — Chapter 11 filings mentioning manufacturing/industrial
  3. Bid4Assets     — real estate auction listings (county tax sales / REO)
  4. Tiger Group    — follows deep links from their auctions index

Crexi, LoopNet, Hilco, Heritage, Gordon Brothers:
  blocked or corporate-only pages → manual quick-search links in email.
"""

import json
import logging
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

EAST_COAST_STATES = [
    "NY", "NJ", "CT", "MA", "RI", "NH", "ME", "VT",
    "PA", "MD", "DE", "VA", "NC", "SC", "GA", "FL", "DC",
]

MFG_KEYWORDS = [
    "manufactur", "industrial", "fabricat", "warehouse",
    "distribution", "assembly", "production", "plant", "mill",
    "factory", "flex", "light industrial", "processing",
    "equipment", "machinery",
]

_90_DAYS_AGO = (date.today() - timedelta(days=90)).isoformat()
_EC_LOWER = [s.lower() for s in EAST_COAST_STATES]


@dataclass
class Listing:
    source: str
    title: str
    url: str
    location: str = ""
    price: str = ""
    size_sf: str = ""
    property_type: str = ""
    description: str = ""
    raw: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────

def _get(url: str, **kwargs) -> requests.Response | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, **kwargs)
        log.debug("GET %s → %s", url, r.status_code)
        return r
    except Exception as exc:
        log.warning("GET %s failed: %s", url, exc)
        return None


def _links(html: str, base: str) -> list[dict]:
    """Return every non-trivial link on a page as {text, url, context}."""
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if not text or len(text) < 5:
            continue
        href = a["href"]
        if any(href.startswith(p) for p in ("mailto:", "tel:", "#", "javascript:")):
            continue
        url = href if href.startswith("http") else urllib.parse.urljoin(base, href)
        if url in seen:
            continue
        seen.add(url)
        ctx = a.parent.get_text(" ", strip=True) if a.parent else ""
        out.append({"text": text, "url": url, "context": ctx})
    return out


def _has_kw(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in MFG_KEYWORDS)


# ─────────────────────────────────────────────────────────────────────
# 1. CourtListener — public PACER search results (HTML)
#    Filter for actual /docket/ links instead of guessing CSS classes.
# ─────────────────────────────────────────────────────────────────────

_CL_QUERIES = [
    "manufacturing bankruptcy",
    "industrial facility chapter 11",
    "warehouse distribution bankruptcy",
    "factory liquidation chapter 7",
]


def scrape_courtlistener() -> list[Listing]:
    listings, seen = [], set()

    for query in _CL_QUERIES:
        resp = _get(
            "https://www.courtlistener.com/",
            params={"q": query, "type": "d", "order_by": "date_filed desc"},
        )
        if not resp or resp.status_code != 200:
            continue

        for lnk in _links(resp.text, "https://www.courtlistener.com/"):
            # CourtListener docket URLs look like /docket/XXXXXXXX/case-name/
            if "/docket/" not in lnk["url"]:
                continue
            if lnk["url"] in seen:
                continue
            seen.add(lnk["url"])

            listings.append(Listing(
                source="CourtListener (PACER)",
                title=lnk["text"][:200],
                url=lnk["url"],
                property_type="Bankruptcy Docket",
                description=f"Query: {query} · {lnk['context'][:200]}",
            ))

    log.info("CourtListener: %d dockets", len(listings))
    return listings


# ─────────────────────────────────────────────────────────────────────
# 2. SEC EDGAR — Chapter 11 filings mentioning manufacturing/industrial
#    Uses EDGAR's free full-text search API (no auth required).
# ─────────────────────────────────────────────────────────────────────

_EDGAR_API = "https://efts.sec.gov/LATEST/search-index"


def scrape_edgar() -> list[Listing]:
    listings, seen = [], set()

    search_terms = [
        '"chapter 11" "manufacturing facility"',
        '"chapter 11" "industrial property"',
        '"chapter 7" "manufacturing" "real estate"',
        '"bankruptcy" "industrial" "East Coast"',
    ]

    for term in search_terms:
        try:
            resp = requests.get(
                _EDGAR_API,
                params={
                    "q": term,
                    "dateRange": "custom",
                    "startdt": _90_DAYS_AGO,
                    "forms": "8-K,8-K/A",
                    "_source": "file_date,period_of_report,entity_name,file_num,period_of_report",
                },
                headers={**HEADERS, "Accept": "application/json"},
                timeout=20,
            )
            if resp.status_code != 200:
                log.debug("EDGAR %r → %s", term, resp.status_code)
                continue

            hits = resp.json().get("hits", {}).get("hits") or []
            for h in hits:
                src = h.get("_source") or {}
                entity = src.get("entity_name") or src.get("display_names") or "Unknown"
                if isinstance(entity, list):
                    entity = entity[0] if entity else "Unknown"
                filed = src.get("file_date") or src.get("period_of_report") or ""
                file_num = src.get("file_num") or h.get("_id") or ""
                url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum={file_num}&type=8-K&dateb=&owner=include&count=10" if file_num else "https://www.sec.gov/cgi-bin/browse-edgar"

                uid = str(h.get("_id") or entity)
                if uid in seen:
                    continue
                seen.add(uid)

                listings.append(Listing(
                    source="SEC EDGAR",
                    title=f"{entity}",
                    url=url,
                    property_type="Chapter 11 / Bankruptcy (SEC Filing)",
                    description=f"Filed {filed} · search: {term[:60]}",
                ))

        except Exception as exc:
            log.warning("EDGAR %r: %s", term, exc)

    log.info("SEC EDGAR: %d filings", len(listings))
    return listings


# ─────────────────────────────────────────────────────────────────────
# 3. Bid4Assets — real estate auction listings
#    County tax sales and REO properties are publicly listed.
# ─────────────────────────────────────────────────────────────────────

_BID4_PAGES = [
    "https://www.bid4assets.com/",
    "https://www.bid4assets.com/auctions",
]

# Bid4Assets category slugs seen in their nav
_BID4_RE_SLUGS = [
    "/real-estate", "/realestate", "/tax-sales", "/county-tax-sales",
    "/foreclosure", "/reo", "/commercial",
]


def scrape_bid4assets() -> list[Listing]:
    listings, seen = [], set()

    # First: find the real estate section
    re_urls: list[str] = []
    for base in _BID4_PAGES:
        resp = _get(base)
        if not resp or resp.status_code != 200:
            continue
        for lnk in _links(resp.text, base):
            if "bid4assets.com" not in lnk["url"]:
                continue
            if any(slug in lnk["url"].lower() for slug in _BID4_RE_SLUGS):
                re_urls.append(lnk["url"])
        if re_urls:
            break

    if not re_urls:
        log.warning("Bid4Assets: could not find real estate section")
        return listings

    # Second: scrape each real-estate sub-page for auction links
    for re_url in re_urls[:4]:
        resp = _get(re_url)
        if not resp or resp.status_code != 200:
            continue
        soup = BeautifulSoup(resp.text, "lxml")

        # Look for individual auction/lot cards
        for card in soup.select(
            ".auction-card, .lot-item, .listing-item, "
            "article, [class*='auction'], [class*='listing'], "
            "[class*='property'], [class*='lot']"
        ):
            title_el = card.select_one("h2, h3, h4, .title, [class*='title']")
            title = title_el.get_text(" ", strip=True) if title_el else ""
            if not title:
                continue

            link_el = card.find("a", href=True)
            href = link_el["href"] if link_el else ""
            url = (href if href.startswith("http")
                   else urllib.parse.urljoin(re_url, href) if href else re_url)
            if url in seen:
                continue
            seen.add(url)

            desc_el = card.select_one("p, .desc, .address, .location, .detail")
            desc = desc_el.get_text(" ", strip=True) if desc_el else ""
            price_el = card.select_one(".price, .bid, .amount, [class*='price']")
            price = price_el.get_text(strip=True) if price_el else ""

            listings.append(Listing(
                source="Bid4Assets",
                title=title,
                url=url,
                price=price,
                property_type="Auction / Tax Sale / REO",
                description=desc[:300],
            ))

        # Fallback: just find all auction links on the page
        if not listings:
            for lnk in _links(resp.text, re_url):
                if "bid4assets.com" not in lnk["url"]:
                    continue
                if lnk["url"] in seen:
                    continue
                combined = lnk["text"] + " " + lnk["context"]
                if not any(w in combined.lower() for w in
                           ["auction", "property", "real estate", "tax sale",
                            "foreclos", "commercial", "industrial"]):
                    continue
                seen.add(lnk["url"])
                listings.append(Listing(
                    source="Bid4Assets",
                    title=lnk["text"][:200],
                    url=lnk["url"],
                    property_type="Auction / Tax Sale / REO",
                    description=lnk["context"][:300],
                ))

    log.info("Bid4Assets: %d listings", len(listings))
    return listings


# ─────────────────────────────────────────────────────────────────────
# 4. Tiger Group — follow deep links from their auctions index
# ─────────────────────────────────────────────────────────────────────

def scrape_tiger() -> list[Listing]:
    listings, seen = [], set()

    resp = _get("https://www.tigergroup.com/auctions/")
    if not resp or resp.status_code != 200:
        return listings

    # Collect Tiger-domain links from the index page
    sub_urls = [
        lnk["url"] for lnk in _links(resp.text, "https://www.tigergroup.com/auctions/")
        if "tigergroup.com" in lnk["url"]
        and lnk["url"] != "https://www.tigergroup.com/auctions/"
        and not any(lnk["url"].endswith(x) for x in ["/", "#"])
    ]

    # Also probe known sub-paths
    for candidate in [
        "https://www.tigergroup.com/current-sales/",
        "https://www.tigergroup.com/machinery-equipment/",
        "https://www.tigergroup.com/industrial/",
        "https://www.tigergroup.com/real-estate/",
    ]:
        if candidate not in sub_urls:
            sub_urls.append(candidate)

    for url in sub_urls[:12]:
        resp2 = _get(url)
        if not resp2 or resp2.status_code != 200:
            continue
        for lnk in _links(resp2.text, url):
            if lnk["url"] in seen:
                continue
            if not _has_kw(lnk["text"] + " " + lnk["context"]):
                continue
            if "tigergroup.com" not in lnk["url"]:
                continue
            seen.add(lnk["url"])
            listings.append(Listing(
                source="Tiger Group",
                title=lnk["text"][:200],
                url=lnk["url"],
                property_type="Industrial Liquidation Auction",
                description=lnk["context"][:300],
            ))

    log.info("Tiger Group: %d listings", len(listings))
    return listings


# ─────────────────────────────────────────────────────────────────────
# Manual search links — shown as buttons in the digest email
# ─────────────────────────────────────────────────────────────────────

_EC = ",".join(EAST_COAST_STATES)

MANUAL_SEARCH_LINKS = {
    "Crexi — Industrial East Coast":
        f"https://www.crexi.com/properties?types=Industrial&states={_EC}"
        "&sort=PublishedDate&sortDirection=Descending",
    "LoopNet — Industrial East Coast":
        "https://www.loopnet.com/search/industrial-properties/east-coast-usa/for-sale/",
    "CourtListener — Bankruptcy Search":
        "https://www.courtlistener.com/?q=manufacturing+industrial"
        "&type=d&order_by=date_filed+desc",
    "SEC EDGAR — Chapter 11":
        "https://efts.sec.gov/LATEST/search-index?q=%22chapter+11%22+%22manufacturing%22&forms=8-K",
    "Auction.com — Commercial":
        "https://www.auction.com/commercial/",
    "Bid4Assets — Real Estate":
        "https://www.bid4assets.com/",
    "Tiger Group — Auctions":
        "https://www.tigergroup.com/auctions/",
    "Hilco Global — Real Estate":
        "https://hilcoglobal.com/real-estate/",
}
