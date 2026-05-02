"""
Scrapers for WSV deal-flow digest.

Auto-scraped sources (no JS required):
  - CourtListener (PACER) — bankruptcy filings
  - Hilco Global, Tiger Group, Heritage Global, Rabin Worldwide — liquidations
  - Ten-X Commercial, Bid4Assets — foreclosure / REO auctions

Crexi and LoopNet block all automated access (Cloudflare).
They appear as clickable quick-search links in the digest instead.
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
    "factory", "flex space", "light industrial", "processing",
]

_90_DAYS_AGO = (date.today() - timedelta(days=90)).isoformat()


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
# Generic link-based scraper
#
# Instead of guessing CSS class names (which break when sites update),
# we grab every <a> tag on a page and filter by keyword in the
# surrounding text. This is far more robust.
# ─────────────────────────────────────────────────────────────────────

def _scrape_by_links(
    base_url: str,
    source_name: str,
    property_type: str = "Industrial",
    require_ec: bool = False,
) -> list[Listing]:
    """Fetch a page and return all links whose context matches MFG_KEYWORDS."""
    try:
        resp = requests.get(base_url, headers=HEADERS, timeout=25)
        resp.raise_for_status()
        log.debug("%s → HTTP %s, %d bytes", source_name, resp.status_code, len(resp.content))
    except Exception as exc:
        log.warning("%s fetch failed: %s", source_name, exc)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    listings: list[Listing] = []
    seen: set[str] = set()

    for link in soup.find_all("a", href=True):
        link_text = link.get_text(" ", strip=True)
        if not link_text or len(link_text) < 8:
            continue

        # Gather surrounding context: parent text up two levels
        ctx_parts = [link_text]
        node = link.parent
        for _ in range(2):
            if node:
                ctx_parts.append(node.get_text(" ", strip=True))
                node = node.parent
        context = " ".join(ctx_parts).lower()

        if not any(kw in context for kw in MFG_KEYWORDS):
            continue

        if require_ec:
            state_match = (
                any(s.lower() in context for s in EAST_COAST_STATE_NAMES)
                or any(f" {s.lower()} " in f" {context} " for s in EAST_COAST_STATES)
            )
            if not state_match:
                continue

        href = link["href"]
        # Skip mailto, tel, anchor-only, javascript links
        if any(href.startswith(p) for p in ("mailto:", "tel:", "#", "javascript:")):
            continue
        url = href if href.startswith("http") else urllib.parse.urljoin(base_url, href)

        if url in seen or url == base_url:
            continue
        seen.add(url)

        # Use parent text as description, capped
        parent_text = link.parent.get_text(" ", strip=True) if link.parent else ""

        listings.append(Listing(
            source=source_name,
            title=link_text[:200],
            url=url,
            property_type=property_type,
            description=parent_text[:300],
        ))

    log.info("%s: %d listings from %s", source_name, len(listings), base_url)
    return listings


# ─────────────────────────────────────────────────────────────────────
# CourtListener — free PACER bankruptcy dockets
# Uses the /dockets/ REST endpoint with correct filter params.
# ─────────────────────────────────────────────────────────────────────

def scrape_courtlistener() -> list[Listing]:
    session = requests.Session()
    seen: set[str] = set()
    listings: list[Listing] = []

    # Use the /dockets/ endpoint with date_filed__gte (not "filed_after")
    for term in ["manufacturing", "industrial", "warehouse", "fabrication"]:
        try:
            resp = session.get(
                "https://www.courtlistener.com/api/rest/v4/dockets/",
                params={
                    "q": term,
                    "date_filed__gte": _90_DAYS_AGO,
                    "order_by": "date_filed desc",
                    "page_size": 20,
                },
                headers={**HEADERS, "Accept": "application/json"},
                timeout=20,
            )
            log.debug("CourtListener '%s' → HTTP %s", term, resp.status_code)
            if resp.status_code != 200:
                continue

            data = resp.json()
            results = data.get("results") or []
            log.debug("CourtListener '%s' → %d results", term, len(results))

            for r in results:
                rel = r.get("absolute_url") or ""
                url = rel if rel.startswith("http") else "https://www.courtlistener.com" + rel
                if url in seen:
                    continue
                seen.add(url)

                case_name = r.get("case_name") or r.get("caseName") or "Unknown"
                docket_num = r.get("docket_number") or r.get("docketNumber") or ""
                filed = r.get("date_filed") or r.get("dateFiled") or ""
                court = (r.get("court") or r.get("court_id") or "").upper()

                listings.append(Listing(
                    source="CourtListener (PACER)",
                    title=case_name,
                    url=url,
                    location=court,
                    property_type="Bankruptcy Filing",
                    description=f"Docket {docket_num} · Filed {filed} · keyword: {term}",
                ))

        except Exception as exc:
            log.warning("CourtListener '%s': %s", term, exc)

    log.info("CourtListener: %d unique filings", len(listings))
    return listings


# ─────────────────────────────────────────────────────────────────────
# Individual site scrapers (all use the generic link scraper)
# ─────────────────────────────────────────────────────────────────────

def scrape_hilco() -> list[Listing]:
    results = []
    for url in [
        "https://hilcoglobal.com/service/industrial/",
        "https://hilcoglobal.com/recent-transactions/",
    ]:
        results += _scrape_by_links(url, "Hilco Global", "Industrial Liquidation")
    # Deduplicate across pages
    seen, unique = set(), []
    for l in results:
        if l.url not in seen:
            seen.add(l.url)
            unique.append(l)
    return unique


def scrape_tiger() -> list[Listing]:
    return _scrape_by_links(
        "https://www.tigergroup.com/auctions/",
        "Tiger Group",
        "Industrial Liquidation Auction",
    )


def scrape_heritage() -> list[Listing]:
    return _scrape_by_links(
        "https://www.hgp.com/auctions",
        "Heritage Global",
        "Industrial / Commercial Auction",
    )


def scrape_rabin() -> list[Listing]:
    return _scrape_by_links(
        "https://www.rabinworldwide.com/auctions/",
        "Rabin Worldwide",
        "Industrial Plant Auction",
    )


def scrape_tenx() -> list[Listing]:
    return _scrape_by_links(
        "https://www.ten-x.com/listings/",
        "Ten-X Commercial",
        "Commercial Auction / REO",
        require_ec=True,
    )


def scrape_bid4assets() -> list[Listing]:
    return _scrape_by_links(
        "https://www.bid4assets.com/real-estate",
        "Bid4Assets",
        "Foreclosure / Government Auction",
        require_ec=True,
    )


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
        "https://www.courtlistener.com/?type=d&q=manufacturing+industrial"
        "&order_by=date_filed+desc",
    "Auction.com — Commercial":
        "https://www.auction.com/commercial/",
    "Ten-X — Commercial Auctions":
        "https://www.ten-x.com/listings/",
    "Bid4Assets — Real Estate":
        "https://www.bid4assets.com/real-estate",
}
