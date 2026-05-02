"""
Scrapers for WSV deal-flow digest.

Sources:
  - CourtListener  — scrapes public HTML search results (no auth needed)
  - Tiger Group    — follows sub-links from auctions page
  - Hilco Global   — correct URLs
  - Heritage Global — correct URL + follow transactions link
  - Bid4Assets     — correct URL for real estate auctions
  - Gordon Brothers — industrial liquidations

Crexi/LoopNet/Ten-X block all automation → manual quick-search links only.
"""

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

EAST_COAST_STATE_NAMES = [
    "new york", "new jersey", "connecticut", "massachusetts",
    "rhode island", "new hampshire", "maine", "vermont",
    "pennsylvania", "maryland", "delaware", "virginia",
    "north carolina", "south carolina", "georgia", "florida",
]

MFG_KEYWORDS = [
    "manufactur", "industrial", "fabricat", "warehouse",
    "distribution", "assembly", "production", "plant", "mill",
    "factory", "flex", "light industrial", "processing", "equipment",
    "machinery", "commercial real estate", "real estate",
]


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
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _get(url: str, **kwargs) -> requests.Response | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20, **kwargs)
        log.debug("%s → HTTP %s", url, resp.status_code)
        return resp
    except Exception as exc:
        log.warning("GET %s failed: %s", url, exc)
        return None


def _links_from_page(html: str, base_url: str) -> list[dict]:
    """Return all non-trivial links from a page as {text, url} dicts."""
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if not text or len(text) < 6:
            continue
        href = a["href"]
        if any(href.startswith(p) for p in ("mailto:", "tel:", "#", "javascript:")):
            continue
        full = href if href.startswith("http") else urllib.parse.urljoin(base_url, href)
        if full in seen:
            continue
        seen.add(full)
        parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
        results.append({"text": text, "url": full, "context": parent_text})
    return results


def _has_keyword(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in MFG_KEYWORDS)


def _is_east_coast(text: str) -> bool:
    t = text.lower()
    return (any(s.lower() in t for s in EAST_COAST_STATE_NAMES)
            or any(f" {s} " in f" {t} " for s in EAST_COAST_STATES))


# ─────────────────────────────────────────────────────────────────────
# CourtListener — HTML search (public, no auth required)
# ─────────────────────────────────────────────────────────────────────

_CL_SEARCHES = [
    "manufacturing bankruptcy",
    "industrial facility bankruptcy",
    "warehouse bankruptcy",
    "factory liquidation",
]

def scrape_courtlistener() -> list[Listing]:
    listings: list[Listing] = []
    seen: set[str] = set()

    for query in _CL_SEARCHES:
        resp = _get(
            "https://www.courtlistener.com/",
            params={"q": query, "type": "d", "order_by": "date_filed desc"},
        )
        if not resp or resp.status_code != 200:
            continue

        soup = BeautifulSoup(resp.text, "lxml")

        # CourtListener search results are in <article> or .result blocks
        for result in soup.select("article, .result, [class*='search-result'], li.pointer"):
            title_el = result.select_one("h3 a, h4 a, .case-name a, a[href*='/docket/']")
            if not title_el:
                continue
            title = title_el.get_text(" ", strip=True)
            href = title_el.get("href", "")
            url = href if href.startswith("http") else "https://www.courtlistener.com" + href
            if url in seen:
                continue
            seen.add(url)

            desc_el = result.select_one("p, .snippet, .description, .meta")
            desc = desc_el.get_text(" ", strip=True) if desc_el else ""

            court_el = result.select_one(".court, [class*='court'], .jurisdiction")
            court = court_el.get_text(strip=True) if court_el else ""

            date_el = result.select_one("time, .date, [class*='date']")
            filed = date_el.get_text(strip=True) if date_el else ""

            listings.append(Listing(
                source="CourtListener (PACER)",
                title=title,
                url=url,
                location=court,
                property_type="Bankruptcy Filing",
                description=f"Filed {filed} · {desc[:200]}".strip(" ·"),
            ))

    log.info("CourtListener: %d filings", len(listings))
    return listings


# ─────────────────────────────────────────────────────────────────────
# Tiger Group
# Their /auctions/ page loads fine. Follow sub-links one level deep
# to find actual sale listings.
# ─────────────────────────────────────────────────────────────────────

def scrape_tiger() -> list[Listing]:
    listings: list[Listing] = []
    seen: set[str] = set()

    # First pass: get sub-links from the auctions index
    resp = _get("https://www.tigergroup.com/auctions/")
    if not resp or resp.status_code != 200:
        return listings

    sub_links = _links_from_page(resp.text, "https://www.tigergroup.com/auctions/")

    # Follow links that look like individual sale/auction pages
    auction_urls = [
        lnk["url"] for lnk in sub_links
        if "tigergroup.com" in lnk["url"]
        and any(kw in lnk["url"].lower() for kw in
                ["auction", "sale", "asset", "machinery", "equipment",
                 "industrial", "manufactur", "liquidat"])
    ]

    # Also try direct listing URL patterns
    for candidate in [
        "https://www.tigergroup.com/current-sales/",
        "https://www.tigergroup.com/sales/",
        "https://www.tigergroup.com/events/",
    ]:
        if candidate not in [l["url"] for l in sub_links]:
            auction_urls.append(candidate)

    # Scrape each candidate page
    for url in auction_urls[:10]:
        resp2 = _get(url)
        if not resp2 or resp2.status_code != 200:
            continue
        for lnk in _links_from_page(resp2.text, url):
            if lnk["url"] in seen:
                continue
            if not _has_keyword(lnk["text"] + " " + lnk["context"]):
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
# Hilco Global  (correct URLs)
# ─────────────────────────────────────────────────────────────────────

_HILCO_URLS = [
    "https://hilcoglobal.com/real-estate/",
    "https://hilcoglobal.com/",
]

def scrape_hilco() -> list[Listing]:
    listings: list[Listing] = []
    seen: set[str] = set()

    for base in _HILCO_URLS:
        resp = _get(base)
        if not resp or resp.status_code != 200:
            continue
        for lnk in _links_from_page(resp.text, base):
            if lnk["url"] in seen:
                continue
            if not _has_keyword(lnk["text"] + " " + lnk["context"]):
                continue
            if "hilcoglobal.com" not in lnk["url"]:
                continue
            seen.add(lnk["url"])
            listings.append(Listing(
                source="Hilco Global",
                title=lnk["text"][:200],
                url=lnk["url"],
                property_type="Industrial Liquidation",
                description=lnk["context"][:300],
            ))

    log.info("Hilco Global: %d listings", len(listings))
    return listings


# ─────────────────────────────────────────────────────────────────────
# Heritage Global  (hgp.com)
# /transactions/ is the right path based on the nav links seen
# ─────────────────────────────────────────────────────────────────────

_HGP_URLS = [
    "https://www.hgp.com/transactions/",
    "https://www.hgp.com/",
]

def scrape_heritage() -> list[Listing]:
    listings: list[Listing] = []
    seen: set[str] = set()

    for base in _HGP_URLS:
        resp = _get(base)
        if not resp or resp.status_code != 200:
            continue
        for lnk in _links_from_page(resp.text, base):
            if lnk["url"] in seen:
                continue
            if not _has_keyword(lnk["text"] + " " + lnk["context"]):
                continue
            if "hgp.com" not in lnk["url"]:
                continue
            seen.add(lnk["url"])
            listings.append(Listing(
                source="Heritage Global",
                title=lnk["text"][:200],
                url=lnk["url"],
                property_type="Industrial / Commercial Auction",
                description=lnk["context"][:300],
            ))

    log.info("Heritage Global: %d listings", len(listings))
    return listings


# ─────────────────────────────────────────────────────────────────────
# Bid4Assets — real estate auction channel
# ─────────────────────────────────────────────────────────────────────

_BID4_URLS = [
    "https://www.bid4assets.com/",
    "https://www.bid4assets.com/auctions",
]

def scrape_bid4assets() -> list[Listing]:
    listings: list[Listing] = []
    seen: set[str] = set()

    for base in _BID4_URLS:
        resp = _get(base)
        if not resp or resp.status_code != 200:
            continue
        for lnk in _links_from_page(resp.text, base):
            if lnk["url"] in seen:
                continue
            combined = lnk["text"] + " " + lnk["context"]
            if not _has_keyword(combined):
                continue
            seen.add(lnk["url"])
            listings.append(Listing(
                source="Bid4Assets",
                title=lnk["text"][:200],
                url=lnk["url"],
                property_type="Foreclosure / Government Auction",
                description=lnk["context"][:300],
            ))

    log.info("Bid4Assets: %d listings", len(listings))
    return listings


# ─────────────────────────────────────────────────────────────────────
# Gordon Brothers — industrial liquidations
# ─────────────────────────────────────────────────────────────────────

def scrape_gordon_brothers() -> list[Listing]:
    listings: list[Listing] = []
    seen: set[str] = set()

    for base in ["https://www.gordonbrothers.com/services/assets/",
                 "https://www.gordonbrothers.com/"]:
        resp = _get(base)
        if not resp or resp.status_code != 200:
            continue
        for lnk in _links_from_page(resp.text, base):
            if lnk["url"] in seen:
                continue
            if not _has_keyword(lnk["text"] + " " + lnk["context"]):
                continue
            if "gordonbrothers.com" not in lnk["url"]:
                continue
            seen.add(lnk["url"])
            listings.append(Listing(
                source="Gordon Brothers",
                title=lnk["text"][:200],
                url=lnk["url"],
                property_type="Industrial Liquidation",
                description=lnk["context"][:300],
            ))

    log.info("Gordon Brothers: %d listings", len(listings))
    return listings


# ─────────────────────────────────────────────────────────────────────
# Manual search links — rendered as buttons in the digest email
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
    "Auction.com — Commercial":
        "https://www.auction.com/commercial/",
    "Bid4Assets — Real Estate":
        "https://www.bid4assets.com/",
    "Tiger Group — Auctions":
        "https://www.tigergroup.com/auctions/",
}
