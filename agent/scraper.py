"""
Scrapers for WSV deal-flow digest.

  1. SEC EDGAR       — Chapter 11/7 filings mentioning manufacturing/industrial
  2. Bid4Assets      — county tax sales and REO real estate auctions
  3. Tiger Group     — follows Machinery & Equipment sub-page for listings
  4. Justia          — public bankruptcy docket search (no login required)

Crexi / LoopNet / CourtListener (login wall) → manual quick-search links only.
"""

import logging
import re
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
        r = requests.get(url, headers=HEADERS, timeout=20, **kwargs)
        log.debug("GET %s → %s", url, r.status_code)
        return r
    except Exception as exc:
        log.warning("GET %s: %s", url, exc)
        return None


def _links(html: str, base: str) -> list[dict]:
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


def _clean_entity(raw) -> str:
    """Parse SEC entity names which come back as strings like
    \"['Company Name  (CIK 0001234567)']\" """
    if isinstance(raw, list):
        raw = raw[0] if raw else "Unknown"
    s = str(raw).strip("[]'\" ")
    # Strip CIK and exchange suffixes
    s = re.sub(r'\s*\(CIK\s*\d+\)', '', s)
    s = re.sub(r'\s*\(NRDE\)', '', s)
    return s.strip() or "Unknown"


# ─────────────────────────────────────────────────────────────────────
# 1. SEC EDGAR — Chapter 11/7 manufacturing bankruptcies
#    Free full-text search API, no auth required.
#    Returns 8-K filings (material events, incl. bankruptcy).
# ─────────────────────────────────────────────────────────────────────

_EDGAR_QUERIES = [
    '"chapter 11" "manufacturing"',
    '"chapter 11" "industrial"',
    '"chapter 7" "manufacturing facility"',
    '"bankruptcy" "industrial plant"',
    '"chapter 11" "warehouse"',
]


def scrape_edgar() -> list[Listing]:
    listings, seen = [], set()

    for query in _EDGAR_QUERIES:
        try:
            resp = requests.get(
                "https://efts.sec.gov/LATEST/search-index",
                params={
                    "q": query,
                    "forms": "8-K,8-K/A",
                    "dateRange": "custom",
                    "startdt": _90_DAYS_AGO,
                },
                headers={**HEADERS, "Accept": "application/json"},
                timeout=20,
            )
            if resp.status_code != 200:
                log.debug("EDGAR %r → %s", query, resp.status_code)
                continue

            hits = resp.json().get("hits", {}).get("hits") or []
            log.debug("EDGAR %r → %d hits", query, len(hits))

            for h in hits:
                src = h.get("_source") or {}

                # Date filter — API doesn't always honour startdt
                file_date = src.get("file_date") or src.get("period_of_report") or ""
                if file_date and file_date < _90_DAYS_AGO:
                    continue

                entity = _clean_entity(
                    src.get("display_names") or src.get("entity_name")
                )

                # Build a link to the company's recent 8-K filings
                cik_raw = src.get("ciks") or []
                cik = cik_raw[0] if cik_raw else None
                if cik:
                    url = (
                        f"https://www.sec.gov/cgi-bin/browse-edgar"
                        f"?action=getcompany&CIK={cik}"
                        f"&type=8-K&dateb=&owner=include&count=5"
                    )
                else:
                    url = "https://www.sec.gov/cgi-bin/browse-edgar"

                uid = f"{entity}|{file_date}"
                if uid in seen:
                    continue
                seen.add(uid)

                listings.append(Listing(
                    source="SEC EDGAR",
                    title=entity,
                    url=url,
                    property_type="Chapter 11 / Bankruptcy (8-K Filing)",
                    description=f"Filed {file_date} · search: {query}",
                ))

        except Exception as exc:
            log.warning("EDGAR %r: %s", query, exc)

    log.info("SEC EDGAR: %d filings", len(listings))
    return listings


# ─────────────────────────────────────────────────────────────────────
# 2. Bid4Assets — county tax sales and REO real estate
#    Homepage has "County Tax Sales" link → follow it → parse listings.
# ─────────────────────────────────────────────────────────────────────

def scrape_bid4assets() -> list[Listing]:
    listings, seen = [], set()

    # Step 1: find the real-estate / tax-sale section URL from homepage
    home = _get("https://www.bid4assets.com/")
    if not home or home.status_code != 200:
        return listings

    re_urls = []
    for lnk in _links(home.text, "https://www.bid4assets.com/"):
        t = lnk["text"].lower()
        u = lnk["url"].lower()
        if any(kw in t or kw in u for kw in
               ["tax sale", "county", "real estate", "foreclos", "reo", "property"]):
            if "bid4assets.com" in lnk["url"]:
                re_urls.append(lnk["url"])

    if not re_urls:
        log.warning("Bid4Assets: no real-estate section links found on homepage")
        return listings

    log.debug("Bid4Assets RE urls: %s", re_urls[:4])

    # Step 2: scrape each section page for individual auction listings
    for section_url in re_urls[:5]:
        resp = _get(section_url)
        if not resp or resp.status_code != 200:
            continue

        soup = BeautifulSoup(resp.text, "lxml")

        # Try structured cards first
        cards = soup.select(
            ".auction-item, .lot-item, .listing, "
            "[class*='auction'], [class*='listing'], [class*='property'], "
            "article, li.item"
        )
        for card in cards:
            title_el = card.select_one("h2,h3,h4,[class*='title'],[class*='name']")
            title = title_el.get_text(" ", strip=True) if title_el else ""
            if not title or len(title) < 5:
                continue
            link_el = card.find("a", href=True)
            href = link_el["href"] if link_el else ""
            url = (href if href.startswith("http")
                   else urllib.parse.urljoin(section_url, href) if href
                   else section_url)
            if url in seen:
                continue
            seen.add(url)
            desc = card.get_text(" ", strip=True)[:300]
            listings.append(Listing(
                source="Bid4Assets",
                title=title,
                url=url,
                property_type="Auction / Tax Sale / REO",
                description=desc,
            ))

        # Fallback: just collect all links that look like individual auction pages
        if not listings:
            for lnk in _links(resp.text, section_url):
                if lnk["url"] in seen:
                    continue
                if "bid4assets.com" not in lnk["url"]:
                    continue
                # Auction pages often have numeric IDs in the URL
                if not re.search(r'/\d{4,}', lnk["url"]):
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
# 3. Tiger Group — follow Machinery & Equipment sub-page
# ─────────────────────────────────────────────────────────────────────

_TIGER_CANDIDATES = [
    "https://www.tigergroup.com/machinery-equipment/",
    "https://www.tigergroup.com/current-sales/",
    "https://www.tigergroup.com/auctions/industrial/",
    "https://www.tigergroup.com/industrial/",
]


def scrape_tiger() -> list[Listing]:
    listings, seen = [], set()

    # Find Machinery & Equipment link from main auctions page
    index = _get("https://www.tigergroup.com/auctions/")
    if index and index.status_code == 200:
        for lnk in _links(index.text, "https://www.tigergroup.com/auctions/"):
            if "tigergroup.com" not in lnk["url"]:
                continue
            t = lnk["text"].lower()
            if any(kw in t for kw in ["machinery", "equipment", "industrial",
                                       "manufactur", "sale", "auction"]):
                if lnk["url"] not in _TIGER_CANDIDATES:
                    _TIGER_CANDIDATES.insert(0, lnk["url"])

    for url in _TIGER_CANDIDATES[:6]:
        resp = _get(url)
        if not resp or resp.status_code != 200:
            continue

        # Look for individual sale/listing links with meaningful titles
        for lnk in _links(resp.text, url):
            if lnk["url"] in seen:
                continue
            if "tigergroup.com" not in lnk["url"]:
                continue
            # Skip pure nav/footer links
            if lnk["url"] in ("https://www.tigergroup.com/",
                               "https://www.tigergroup.com/auctions/"):
                continue
            combined = lnk["text"] + " " + lnk["context"]
            if not any(kw in combined.lower() for kw in MFG_KEYWORDS):
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
# 4. Justia Dockets — public bankruptcy search (no login)
# ─────────────────────────────────────────────────────────────────────

_JUSTIA_QUERIES = [
    "manufacturing industrial",
    "industrial facility",
    "warehouse distribution",
]


def scrape_justia() -> list[Listing]:
    listings, seen = [], set()

    for query in _JUSTIA_QUERIES:
        resp = _get(
            "https://dockets.justia.com/search",
            params={
                "query": query,
                "court_type": "bk",
                "nature_of_suit": "",
                "sort": "date_filed",
                "order": "desc",
            },
        )
        if not resp or resp.status_code != 200:
            continue

        for lnk in _links(resp.text, "https://dockets.justia.com/"):
            if "/docket/" not in lnk["url"]:
                continue
            if "justia.com" not in lnk["url"]:
                continue
            if lnk["url"] in seen:
                continue
            seen.add(lnk["url"])
            listings.append(Listing(
                source="Justia (Bankruptcy)",
                title=lnk["text"][:200],
                url=lnk["url"],
                property_type="Bankruptcy Docket",
                description=lnk["context"][:300],
            ))

    log.info("Justia: %d dockets", len(listings))
    return listings


# ─────────────────────────────────────────────────────────────────────
# Manual search links
# ─────────────────────────────────────────────────────────────────────

_EC = ",".join(EAST_COAST_STATES)

MANUAL_SEARCH_LINKS = {
    "Crexi — Industrial East Coast":
        f"https://www.crexi.com/properties?types=Industrial&states={_EC}"
        "&sort=PublishedDate&sortDirection=Descending",
    "LoopNet — Industrial East Coast":
        "https://www.loopnet.com/search/industrial-properties/east-coast-usa/for-sale/",
    "SEC EDGAR — Chapter 11 Filings":
        "https://efts.sec.gov/LATEST/search-index?q=%22chapter+11%22+%22manufacturing%22&forms=8-K",
    "Auction.com — Commercial":
        "https://www.auction.com/commercial/",
    "Bid4Assets — Tax Sales":
        "https://www.bid4assets.com/",
    "Tiger Group — Auctions":
        "https://www.tigergroup.com/auctions/",
    "Hilco Global — Real Estate":
        "https://hilcoglobal.com/real-estate/",
    "CourtListener — Bankruptcy":
        "https://www.courtlistener.com/?q=manufacturing+industrial&type=d&order_by=date_filed+desc",
}
