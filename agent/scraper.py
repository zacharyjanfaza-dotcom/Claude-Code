"""
Scrapes publicly accessible sources for:
  1. Bankruptcy filings — CourtListener (PACER)
  2. Industrial liquidation auctions — Hilco Global, Tiger Group,
     Heritage Global, Rabin Worldwide
  3. Commercial foreclosure auctions — Ten-X Commercial, Bid4Assets

Crexi and LoopNet block automated access (Cloudflare). Pre-built
search links for those are returned as MANUAL_SEARCH_LINKS so they
appear as clickable shortcuts in the weekly digest.
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
    "New York", "New Jersey", "Connecticut", "Massachusetts", "Rhode Island",
    "New Hampshire", "Maine", "Vermont", "Pennsylvania", "Maryland",
    "Delaware", "Virginia", "North Carolina", "South Carolina",
    "Georgia", "Florida", "Washington DC",
]

MFG_KEYWORDS = [
    "manufactur", "industrial", "fabricat", "processing", "warehouse",
    "distribution", "assembly", "production", "plant", "mill", "factory",
    "flex", "light industrial", "commercial real estate",
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


# ─────────────────────────────────────────────
# CourtListener — free PACER bankruptcy search
# ─────────────────────────────────────────────

def scrape_courtlistener() -> list[Listing]:
    """Free public API for PACER dockets. Searches for bankruptcy filings
    involving manufacturing, industrial, and warehouse assets."""
    session = requests.Session()
    seen: set[str] = set()
    listings: list[Listing] = []

    for term in ["manufacturing", "industrial building", "warehouse", "fabrication plant"]:
        try:
            resp = session.get(
                "https://www.courtlistener.com/api/rest/v4/search/",
                params={
                    "type": "d",
                    "q": term,
                    "order_by": "date_filed desc",
                    "filed_after": _90_DAYS_AGO,
                    "page_size": 20,
                },
                headers={**HEADERS, "Accept": "application/json"},
                timeout=20,
            )
            if resp.status_code != 200:
                log.debug("CourtListener '%s' → %s", term, resp.status_code)
                continue
            for r in resp.json().get("results") or []:
                rel = r.get("absolute_url") or ""
                url = rel if rel.startswith("http") else "https://www.courtlistener.com" + rel
                if url in seen:
                    continue
                seen.add(url)
                name = r.get("caseName") or r.get("case_name") or "Unknown"
                listings.append(Listing(
                    source="CourtListener (PACER)",
                    title=name,
                    url=url,
                    location=(r.get("court") or r.get("court_id") or "").upper(),
                    property_type="Bankruptcy Filing",
                    description=(
                        f"Docket {r.get('docketNumber') or r.get('docket_number') or ''} · "
                        f"Filed {r.get('dateFiled') or r.get('date_filed') or ''} · "
                        f"keyword: {term}"
                    ),
                ))
        except Exception as exc:
            log.warning("CourtListener '%s': %s", term, exc)

    log.info("CourtListener: %d filings", len(listings))
    return listings


# ─────────────────────────────────────────────
# Ten-X Commercial — commercial property auctions
# (includes bank-owned / REO / lender-controlled)
# ─────────────────────────────────────────────

def scrape_tenx() -> list[Listing]:
    session = requests.Session()
    listings: list[Listing] = []
    try:
        resp = session.get(
            "https://www.ten-x.com/company/blog/listings/",
            headers=HEADERS, timeout=25,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Ten-X embeds listing data as JSON in script tags
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                import json
                data = json.loads(script.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") not in ("Product", "Offer", "RealEstateListing"):
                        continue
                    title = item.get("name") or ""
                    desc = item.get("description") or ""
                    url = item.get("url") or item.get("@id") or "https://www.ten-x.com"
                    addr = item.get("address") or {}
                    state = addr.get("addressRegion") or ""
                    if state and state.upper() not in EAST_COAST_STATES:
                        continue
                    if not any(kw in (title + desc).lower() for kw in MFG_KEYWORDS):
                        continue
                    listings.append(Listing(
                        source="Ten-X Commercial",
                        title=title,
                        url=url,
                        location=_join(addr.get("addressLocality"), state),
                        property_type="Commercial Auction / REO",
                        description=desc[:300],
                    ))
            except Exception:
                pass

        # Fallback: parse visible cards
        if not listings:
            for card in soup.select(".listing-card, .property-card, article, .card"):
                title = _text(card, "h2,h3,h4,[class*='title']")
                if not title:
                    continue
                desc = _text(card, "p,[class*='desc']")
                loc = _text(card, "[class*='location'],[class*='address'],[class*='city']")
                if not any(kw in (title + desc).lower() for kw in MFG_KEYWORDS):
                    continue
                ec = any(s.lower() in (title + loc).lower()
                         for s in EAST_COAST_STATES + EAST_COAST_STATE_NAMES)
                if not ec:
                    continue
                link = card.find("a", href=True)
                href = link["href"] if link else ""
                listings.append(Listing(
                    source="Ten-X Commercial",
                    title=title,
                    url=href if href.startswith("http") else urllib.parse.urljoin("https://www.ten-x.com", href),
                    location=loc,
                    property_type="Commercial Auction / REO",
                    description=desc[:300],
                ))
    except Exception as exc:
        log.warning("Ten-X: %s", exc)

    log.info("Ten-X: %d listings", len(listings))
    return listings


# ─────────────────────────────────────────────
# Bid4Assets — government surplus + foreclosure auctions
# ─────────────────────────────────────────────

def scrape_bid4assets() -> list[Listing]:
    session = requests.Session()
    listings: list[Listing] = []
    try:
        resp = session.get(
            "https://www.bid4assets.com/auctions#category=Real%20Estate",
            headers=HEADERS, timeout=25,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        for card in soup.select(".auction-item, .lot-item, .listing, article, .card"):
            title = _text(card, "h2,h3,h4,[class*='title'],[class*='name']")
            if not title:
                continue
            desc = _text(card, "p,[class*='desc'],[class*='detail']")
            loc = _text(card, "[class*='location'],[class*='address'],[class*='city']")
            text_blob = (title + desc + loc).lower()

            if not any(kw in text_blob for kw in MFG_KEYWORDS):
                continue
            ec = any(s.lower() in text_blob
                     for s in EAST_COAST_STATES + [s.lower() for s in EAST_COAST_STATE_NAMES])
            if not ec:
                continue

            link = card.find("a", href=True)
            href = link["href"] if link else ""
            url = (href if href.startswith("http")
                   else urllib.parse.urljoin("https://www.bid4assets.com", href))
            listings.append(Listing(
                source="Bid4Assets",
                title=title,
                url=url,
                location=loc,
                property_type="Foreclosure / Government Auction",
                description=desc[:300],
            ))
    except Exception as exc:
        log.warning("Bid4Assets: %s", exc)

    log.info("Bid4Assets: %d listings", len(listings))
    return listings


# ─────────────────────────────────────────────
# Hilco Global — industrial liquidations
# ─────────────────────────────────────────────

def scrape_hilco() -> list[Listing]:
    session = requests.Session()
    listings: list[Listing] = []

    for base_url in [
        "https://hilcoglobal.com/service/industrial/",
        "https://hilcoglobal.com/recent-transactions/",
    ]:
        try:
            resp = session.get(base_url, headers=HEADERS, timeout=25)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            for item in soup.select(
                "article,.card,.item,li.post,"
                "[class*='project'],[class*='case'],[class*='auction'],"
                "[class*='listing'],[class*='transaction']"
            ):
                title = _text(item, "h1,h2,h3,h4,[class*='title'],[class*='name']")
                if not title or len(title) < 5:
                    continue
                desc = _text(item, "p,[class*='excerpt'],[class*='desc']")
                if not any(kw in (title + desc).lower() for kw in MFG_KEYWORDS):
                    continue
                link = item.find("a", href=True)
                href = (link["href"] if link else "") or ""
                listings.append(Listing(
                    source="Hilco Global",
                    title=title,
                    url=(href if href.startswith("http")
                         else urllib.parse.urljoin(base_url, href) if href else base_url),
                    location=_text(item, "[class*='location'],[class*='city'],address"),
                    property_type="Industrial Liquidation",
                    description=desc[:300],
                ))
            if listings:
                break
        except Exception as exc:
            log.debug("Hilco %s: %s", base_url, exc)

    log.info("Hilco: %d listings", len(listings))
    return listings


# ─────────────────────────────────────────────
# Tiger Group — industrial auctions
# ─────────────────────────────────────────────

def scrape_tiger() -> list[Listing]:
    session = requests.Session()
    listings: list[Listing] = []

    for base_url in ["https://www.tigergroup.com/auctions/", "https://www.tigergroup.com/"]:
        try:
            resp = session.get(base_url, headers=HEADERS, timeout=25)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            for item in soup.select(
                "article,.card,[class*='auction'],[class*='listing'],"
                "[class*='sale'],[class*='event'],li.item"
            ):
                title = _text(item, "h1,h2,h3,h4,[class*='title']")
                if not title or len(title) < 5:
                    continue
                desc = _text(item, "p,[class*='excerpt'],[class*='desc']")
                loc = _text(item, "[class*='location'],[class*='city'],[class*='state']")
                if not any(kw in (title + desc + loc).lower() for kw in MFG_KEYWORDS):
                    continue
                link = item.find("a", href=True)
                href = (link["href"] if link else "") or ""
                listings.append(Listing(
                    source="Tiger Group",
                    title=title,
                    url=(href if href.startswith("http")
                         else urllib.parse.urljoin(base_url, href) if href else base_url),
                    location=loc,
                    property_type="Industrial Liquidation Auction",
                    description=desc[:300],
                ))
        except Exception as exc:
            log.debug("Tiger %s: %s", base_url, exc)

    log.info("Tiger Group: %d listings", len(listings))
    return listings


# ─────────────────────────────────────────────
# Heritage Global — industrial & commercial auctions
# ─────────────────────────────────────────────

def scrape_heritage() -> list[Listing]:
    session = requests.Session()
    listings: list[Listing] = []
    try:
        resp = session.get(
            "https://www.hgp.com/auctions",
            headers=HEADERS, timeout=25,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        for item in soup.select("article,.auction-card,.card,[class*='auction'],[class*='lot']"):
            title = _text(item, "h2,h3,h4,[class*='title'],[class*='name']")
            if not title or len(title) < 5:
                continue
            desc = _text(item, "p,[class*='desc'],[class*='excerpt']")
            if not any(kw in (title + desc).lower() for kw in MFG_KEYWORDS):
                continue
            link = item.find("a", href=True)
            href = (link["href"] if link else "") or ""
            listings.append(Listing(
                source="Heritage Global",
                title=title,
                url=(href if href.startswith("http")
                     else urllib.parse.urljoin("https://www.hgp.com", href) if href
                     else "https://www.hgp.com/auctions"),
                location=_text(item, "[class*='location'],[class*='city']"),
                property_type="Industrial / Commercial Auction",
                description=desc[:300],
            ))
    except Exception as exc:
        log.warning("Heritage Global: %s", exc)

    log.info("Heritage Global: %d listings", len(listings))
    return listings


# ─────────────────────────────────────────────
# Rabin Worldwide — industrial plant auctions
# ─────────────────────────────────────────────

def scrape_rabin() -> list[Listing]:
    session = requests.Session()
    listings: list[Listing] = []
    try:
        resp = session.get(
            "https://www.rabinworldwide.com/auctions/",
            headers=HEADERS, timeout=25,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        for item in soup.select("article,.card,[class*='auction'],[class*='listing'],li"):
            title = _text(item, "h2,h3,h4,[class*='title'],[class*='name']")
            if not title or len(title) < 5:
                continue
            desc = _text(item, "p,[class*='desc']")
            loc = _text(item, "[class*='location'],[class*='city']")
            if not any(kw in (title + desc).lower() for kw in MFG_KEYWORDS):
                continue
            link = item.find("a", href=True)
            href = (link["href"] if link else "") or ""
            listings.append(Listing(
                source="Rabin Worldwide",
                title=title,
                url=(href if href.startswith("http")
                     else urllib.parse.urljoin("https://www.rabinworldwide.com", href) if href
                     else "https://www.rabinworldwide.com/auctions/"),
                location=loc,
                property_type="Industrial Plant Auction",
                description=desc[:300],
            ))
    except Exception as exc:
        log.warning("Rabin Worldwide: %s", exc)

    log.info("Rabin Worldwide: %d listings", len(listings))
    return listings


# ─────────────────────────────────────────────
# Manual search links (rendered as buttons in the digest)
# Crexi/LoopNet block automated access — these open pre-filtered searches.
# ─────────────────────────────────────────────

_EC = ",".join(EAST_COAST_STATES)

MANUAL_SEARCH_LINKS = {
    "Crexi — Industrial EC":
        f"https://www.crexi.com/properties?types=Industrial&states={_EC}&sort=PublishedDate&sortDirection=Descending",
    "LoopNet — Industrial EC":
        "https://www.loopnet.com/search/industrial-properties/east-coast-usa/for-sale/",
    "CoStar — Industrial EC":
        "https://www.costar.com/",
    "CourtListener — Bankruptcy Search":
        "https://www.courtlistener.com/?type=d&q=manufacturing+industrial&order_by=date_filed+desc",
    "Auction.com — Commercial":
        "https://www.auction.com/commercial/",
    "Ten-X — Commercial Auctions":
        "https://www.ten-x.com/",
}


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _text(tag, selector: str) -> str:
    el = tag.select_one(selector)
    return el.get_text(" ", strip=True) if el else ""


def _join(*parts) -> str:
    return ", ".join(p for p in parts if p)
