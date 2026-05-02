"""
Scrapes two source categories:
  1. Crexi – industrial listings, East Coast (uses Playwright for JS rendering)
  2. Bankruptcy/liquidation for manufacturing facilities
     (CourtListener PACER + Hilco Global + Tiger Group)
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
}

EAST_COAST_STATES = [
    "NY", "NJ", "CT", "MA", "RI", "NH", "ME", "VT",
    "PA", "MD", "DE", "VA", "NC", "SC", "GA", "FL", "DC",
]

MFG_KEYWORDS = [
    "manufactur", "industrial", "fabricat", "processing", "warehouse",
    "distribution", "assembly", "production", "plant", "mill", "factory",
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


# ─────────────────────────────────────────────
# Crexi  (Playwright — loads real JS)
# ─────────────────────────────────────────────

_CREXI_SEARCH = (
    "https://www.crexi.com/properties"
    "?types=Industrial"
    "&states=" + ",".join(EAST_COAST_STATES)
)


def scrape_crexi() -> list[Listing]:
    """
    Uses Playwright to render Crexi's React SPA, waits for listing cards to
    appear, then extracts title / location / price / size / URL from each card.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    listings: list[Listing] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=HEADERS["User-Agent"],
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )

        try:
            page.goto(_CREXI_SEARCH, wait_until="domcontentloaded", timeout=45_000)

            # Wait for at least one listing card to appear
            page.wait_for_selector(
                ".property-card, [data-testid='property-card'], "
                ".asset-card, [class*='PropertyCard'], [class*='ListingCard']",
                timeout=30_000,
            )
        except PWTimeout:
            log.warning("Crexi: timed out waiting for listing cards")
            browser.close()
            return listings

        # Pull the page content after JS has run
        content = page.content()
        browser.close()

    soup = BeautifulSoup(content, "lxml")

    # Try to grab embedded JSON state first (fastest / most complete)
    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data and next_data.string:
        try:
            blob = json.loads(next_data.string)
            assets = (
                _deep_get(blob, "props", "pageProps", "assets")
                or _deep_get(blob, "props", "pageProps", "listings")
                or []
            )
            for a in assets:
                listings.append(Listing(
                    source="Crexi",
                    title=a.get("name") or a.get("title") or "Industrial Property",
                    url=f"https://www.crexi.com/properties/{a.get('id') or a.get('slug', '')}",
                    location=_join(a.get("city"), a.get("state")),
                    price=_fmt_price(a.get("askingPrice") or a.get("price")),
                    size_sf=_fmt_sf(a.get("buildingSize") or a.get("totalSqFt")),
                    property_type="Industrial",
                    description=(a.get("description") or "")[:400],
                    raw=a,
                ))
            if listings:
                log.info("Crexi: %d listings (from JSON state)", len(listings))
                return listings
        except (json.JSONDecodeError, TypeError):
            pass

    # Fall back to parsing rendered HTML cards
    card_selectors = [
        ".property-card",
        "[data-testid='property-card']",
        "[class*='PropertyCard']",
        "[class*='AssetCard']",
        "[class*='ListingCard']",
    ]
    cards = []
    for sel in card_selectors:
        cards = soup.select(sel)
        if cards:
            break

    for card in cards:
        title = _text(card, "h2, h3, h4, [class*='title'], [class*='name']")
        loc = _text(card, "[class*='location'], [class*='address'], [class*='city']")
        price = _text(card, "[class*='price'], [class*='asking']")
        sf = _text(card, "[class*='size'], [class*='sqft'], [class*='buildingSize']")
        link = card.find("a", href=True)
        url = ("https://www.crexi.com" + link["href"]
               if link and link["href"].startswith("/")
               else (link["href"] if link else _CREXI_SEARCH))
        if title:
            listings.append(Listing(
                source="Crexi",
                title=title,
                url=url,
                location=loc,
                price=price,
                size_sf=sf,
                property_type="Industrial",
            ))

    log.info("Crexi: %d listings (from HTML cards)", len(listings))
    return listings


# ─────────────────────────────────────────────
# CourtListener — free public PACER search
# ─────────────────────────────────────────────

_CL_DOCKETS = "https://www.courtlistener.com/api/rest/v4/dockets/"
_NINETY_DAYS_AGO = (date.today() - timedelta(days=90)).isoformat()

# Bankruptcy court slugs for East Coast districts
_EC_BK_COURTS = [
    "nybk", "nysb", "nyeb",          # New York
    "njb",                             # New Jersey
    "ctb",                             # Connecticut
    "mab",                             # Massachusetts
    "pab", "pawb",                     # Pennsylvania
    "mdb",                             # Maryland
    "vab", "vaeb", "vawb",            # Virginia
    "nceb", "ncwb", "ncmb",           # North Carolina
    "scb",                             # South Carolina
    "gab", "ganb",                     # Georgia
    "flsb", "flnb", "flmb",           # Florida
    "deb",                             # Delaware
]


def scrape_courtlistener() -> list[Listing]:
    """
    Searches CourtListener's PACER docket index for recent Chapter 7/11
    bankruptcy filings mentioning industrial or manufacturing assets.
    """
    session = requests.Session()
    seen: set[str] = set()
    listings: list[Listing] = []

    search_terms = [
        "manufacturing facility",
        "industrial building",
        "warehouse distribution",
        "fabrication plant",
        "production facility",
    ]

    for term in search_terms:
        try:
            resp = session.get(
                _CL_DOCKETS,
                params={
                    "q": term,
                    "order_by": "date_filed desc",
                    "date_filed__gte": _NINETY_DAYS_AGO,
                    "page_size": 20,
                },
                headers={**HEADERS, "Accept": "application/json"},
                timeout=20,
            )
            if resp.status_code != 200:
                log.debug("CourtListener returned %s for '%s'", resp.status_code, term)
                continue

            for r in resp.json().get("results") or []:
                case_url = r.get("absolute_url") or ""
                if not case_url.startswith("http"):
                    case_url = "https://www.courtlistener.com" + case_url

                if case_url in seen:
                    continue
                seen.add(case_url)

                case_name = r.get("case_name") or r.get("caseName") or "Unknown"
                docket_num = r.get("docket_number") or ""
                date_filed = r.get("date_filed") or ""
                court = (r.get("court_id") or r.get("court") or "").upper()
                chapter = _guess_chapter(case_name, r)

                listings.append(Listing(
                    source="CourtListener (PACER)",
                    title=f"{case_name}",
                    url=case_url,
                    location=court,
                    property_type=f"Bankruptcy {chapter}",
                    description=f"Docket {docket_num} • Filed {date_filed} • matched: {term}",
                ))

        except Exception as exc:
            log.debug("CourtListener term '%s' failed: %s", term, exc)

    log.info("CourtListener: %d unique filings", len(listings))
    return listings


def _guess_chapter(case_name: str, record: dict) -> str:
    """Best-effort Chapter number from case name or record fields."""
    for field in ("chapter", "nature_of_suit"):
        val = str(record.get(field) or "")
        if "7" in val:
            return "Chapter 7"
        if "11" in val:
            return "Chapter 11"
    name_lower = case_name.lower()
    if "chapter 7" in name_lower:
        return "Chapter 7"
    if "chapter 11" in name_lower:
        return "Chapter 11"
    return "Filing"


# ─────────────────────────────────────────────
# Hilco Global — industrial liquidations
# ─────────────────────────────────────────────

_HILCO_URLS = [
    "https://hilcoglobal.com/service/industrial/",
    "https://www.hilco.com/services/industrial/",
]


def scrape_hilco() -> list[Listing]:
    session = requests.Session()
    listings: list[Listing] = []

    for base_url in _HILCO_URLS:
        try:
            resp = session.get(base_url, headers=HEADERS, timeout=25)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            for item in soup.select(
                ".auction-item, .event-item, article, "
                ".listing, .service-item, .project, li.item, .case-study"
            ):
                title = _text(item, "h2, h3, h4, .title, .name")
                if not title:
                    continue
                desc = _text(item, "p, .excerpt, .description")
                if not any(kw in (title + desc).lower() for kw in MFG_KEYWORDS):
                    continue

                link = item.find("a", href=True)
                href = link["href"] if link else ""
                url = (href if href.startswith("http")
                       else urllib.parse.urljoin(base_url, href) if href
                       else base_url)

                listings.append(Listing(
                    source="Hilco Global",
                    title=title,
                    url=url,
                    location=_text(item, ".location, .address, .city"),
                    property_type="Industrial Liquidation",
                    description=desc[:300],
                ))

            if listings:
                break

        except Exception as exc:
            log.debug("Hilco %s failed: %s", base_url, exc)

    log.info("Hilco: %d listings", len(listings))
    return listings


# ─────────────────────────────────────────────
# Tiger Group — industrial auctions
# ─────────────────────────────────────────────

_TIGER_URLS = [
    "https://www.tigergroup.com/auctions/",
    "https://www.tigergroup.com/upcoming-auctions/",
]


def scrape_tiger() -> list[Listing]:
    session = requests.Session()
    listings: list[Listing] = []

    for base_url in _TIGER_URLS:
        try:
            resp = session.get(base_url, headers=HEADERS, timeout=25)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            for item in soup.select(".auction, .listing-item, article, .event, .sale-item"):
                title = _text(item, "h2, h3, h4, .auction-title, .title")
                if not title:
                    continue
                desc = _text(item, "p, .excerpt, .description")
                loc = _text(item, ".location, .state, .city, .address")
                if not any(kw in (title + desc + loc).lower() for kw in MFG_KEYWORDS):
                    continue

                link = item.find("a", href=True)
                href = link["href"] if link else ""
                url = (href if href.startswith("http")
                       else urllib.parse.urljoin(base_url, href) if href
                       else base_url)

                listings.append(Listing(
                    source="Tiger Group",
                    title=title,
                    url=url,
                    location=loc,
                    property_type="Industrial Liquidation Auction",
                    description=desc[:300],
                ))

        except Exception as exc:
            log.debug("Tiger %s failed: %s", base_url, exc)

    log.info("Tiger Group: %d listings", len(listings))
    return listings


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _text(tag, selector: str) -> str:
    el = tag.select_one(selector)
    return el.get_text(strip=True) if el else ""


def _join(*parts) -> str:
    return ", ".join(p for p in parts if p)


def _fmt_price(val) -> str:
    if val is None:
        return ""
    try:
        n = float(val)
        if n >= 1_000_000:
            return f"${n / 1_000_000:.2f}M"
        if n >= 1_000:
            return f"${n / 1_000:.0f}K"
        return f"${n:,.0f}"
    except (ValueError, TypeError):
        return str(val)


def _fmt_sf(val) -> str:
    if val is None:
        return ""
    try:
        return f"{int(float(val)):,} SF"
    except (ValueError, TypeError):
        return str(val)


def _deep_get(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d
