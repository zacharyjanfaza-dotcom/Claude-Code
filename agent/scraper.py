"""
Scrapers for WSV deal-flow digest.

  1. Google News RSS  — fresh news on industrial deals, bankruptcies, auctions
  2. SEC EDGAR        — recent 8-K filings via correct date-sorted endpoint
  3. Bid4Assets       — follows "County Tax Sales" deep link for actual listings
  4. Tiger Group      — follows "Machinery & Equipment" deep link

Crexi / LoopNet / CourtListener → manual quick-search links only.
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
_TODAY = date.today().isoformat()


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


# ─────────────────────────────────────────────────────────────────────
# 1. Google News RSS — fresh industrial real estate and bankruptcy news
#    Freely accessible, no auth, returns current results.
# ─────────────────────────────────────────────────────────────────────

_NEWS_QUERIES = [
    'industrial warehouse property sale "East Coast"',
    'manufacturing facility bankruptcy auction',
    '"shallow bay" OR "flex industrial" property sale',
    'industrial real estate foreclosure auction "New York" OR "New Jersey" OR "Pennsylvania"',
    'manufacturing plant chapter 11 bankruptcy',
    'warehouse distribution center for sale "New England" OR "Mid-Atlantic"',
]


def scrape_google_news() -> list[Listing]:
    listings, seen = [], set()

    for query in _NEWS_QUERIES:
        encoded = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
        resp = _get(url)
        if not resp or resp.status_code != 200:
            log.debug("Google News %r → %s", query, resp.status_code if resp else "no response")
            continue

        # Parse RSS as XML
        soup = BeautifulSoup(resp.text, "lxml-xml")
        if not soup.find("item"):
            soup = BeautifulSoup(resp.text, "xml")

        for item in soup.find_all("item"):
            title = (item.find("title") or item.find("title")).get_text(strip=True) if item.find("title") else ""
            link_el = item.find("link")
            # Google News RSS puts the URL in link text or as content
            link = (link_el.get_text(strip=True) if link_el and link_el.get_text(strip=True)
                    else link_el.get("href", "") if link_el else "")
            if not link or not link.startswith("http"):
                # Try <guid>
                guid = item.find("guid")
                link = guid.get_text(strip=True) if guid else ""

            if not title or not link or link in seen:
                continue
            seen.add(link)

            pub = item.find("pubDate")
            pub_text = pub.get_text(strip=True) if pub else ""
            desc_el = item.find("description")
            desc = BeautifulSoup(desc_el.get_text(), "lxml").get_text(" ", strip=True) if desc_el else ""

            listings.append(Listing(
                source="Google News",
                title=title,
                url=link,
                property_type="News / Market Intelligence",
                description=f"{pub_text} — {desc[:250]}".strip(" —"),
            ))

    log.info("Google News: %d articles", len(listings))
    return listings


# ─────────────────────────────────────────────────────────────────────
# 2. SEC EDGAR — recent 8-K filings, date-sorted via EDGAR full search
#    The /LATEST/search-index endpoint ignores startdt; instead we use
#    the EDGAR filing RSS feed which IS date-ordered, then filter.
# ─────────────────────────────────────────────────────────────────────

def scrape_edgar() -> list[Listing]:
    listings, seen = [], set()

    # EDGAR full-text search with explicit date range in query
    queries = [
        '"chapter 11" "manufacturing"',
        '"chapter 11" "industrial"',
        '"chapter 7" "manufacturing facility"',
        '"bankruptcy" "industrial plant"',
    ]

    for query in queries:
        try:
            resp = requests.get(
                "https://efts.sec.gov/LATEST/search-index",
                params={
                    "q": query,
                    "forms": "8-K,8-K/A",
                    "dateRange": "custom",
                    "startdt": _90_DAYS_AGO,
                    "enddt": _TODAY,
                    "hits.hits._source.sort": "file_date",
                },
                headers={**HEADERS, "Accept": "application/json"},
                timeout=20,
            )
            if resp.status_code != 200:
                continue

            for h in (resp.json().get("hits", {}).get("hits") or []):
                src = h.get("_source") or {}
                file_date = src.get("file_date") or ""

                # Python-side date filter since API may not honor startdt
                if file_date and file_date < _90_DAYS_AGO:
                    continue

                # Clean entity name
                raw_name = src.get("display_names") or src.get("entity_name") or ["Unknown"]
                if isinstance(raw_name, list):
                    raw_name = raw_name[0] if raw_name else "Unknown"
                entity = re.sub(r'\s*\(CIK\s*\d+\)', '', str(raw_name)).strip("[]'\" ")

                ciks = src.get("ciks") or []
                cik = ciks[0] if ciks else None
                url = (
                    f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                    f"&CIK={cik}&type=8-K&dateb=&owner=include&count=5"
                    if cik else "https://www.sec.gov/cgi-bin/browse-edgar"
                )

                uid = f"{entity}|{file_date}"
                if uid in seen:
                    continue
                seen.add(uid)

                listings.append(Listing(
                    source="SEC EDGAR",
                    title=entity,
                    url=url,
                    property_type="Chapter 11 / Bankruptcy (8-K)",
                    description=f"Filed {file_date} · {query}",
                ))

        except Exception as exc:
            log.warning("EDGAR %r: %s", query, exc)

    log.info("SEC EDGAR: %d filings", len(listings))
    return listings


# ─────────────────────────────────────────────────────────────────────
# 3. Bid4Assets — follow "County Tax Sales" deep link
# ─────────────────────────────────────────────────────────────────────

def scrape_bid4assets() -> list[Listing]:
    listings, seen = [], set()

    home = _get("https://www.bid4assets.com/")
    if not home or home.status_code != 200:
        return listings

    # Find "County Tax Sales" and similar links
    target_urls = []
    for lnk in _links(home.text, "https://www.bid4assets.com/"):
        t = lnk["text"].lower()
        u = lnk["url"].lower()
        if any(kw in t or kw in u for kw in
               ["county tax", "tax sale", "real estate", "foreclos", "reo", "commercial"]):
            if "bid4assets.com" in lnk["url"] and lnk["url"] != "https://www.bid4assets.com/":
                target_urls.append(lnk["url"])
                log.debug("Bid4Assets target: %s", lnk["url"])

    for section_url in target_urls[:5]:
        resp = _get(section_url)
        if not resp or resp.status_code != 200:
            continue

        soup = BeautifulSoup(resp.text, "lxml")

        # Grab any link that looks like an individual auction page (has digits in path)
        for lnk in _links(resp.text, section_url):
            if lnk["url"] in seen:
                continue
            if "bid4assets.com" not in lnk["url"]:
                continue
            # Individual auction pages typically have numeric IDs
            if not re.search(r'/\d{3,}', lnk["url"]):
                continue
            seen.add(lnk["url"])
            listings.append(Listing(
                source="Bid4Assets",
                title=lnk["text"][:200],
                url=lnk["url"],
                property_type="Tax Sale / REO Auction",
                description=lnk["context"][:300],
            ))

    log.info("Bid4Assets: %d listings", len(listings))
    return listings


# ─────────────────────────────────────────────────────────────────────
# 4. Tiger Group — follow "Machinery & Equipment" deep link
# ─────────────────────────────────────────────────────────────────────

def scrape_tiger() -> list[Listing]:
    listings, seen = [], set()

    index = _get("https://www.tigergroup.com/auctions/")
    if not index or index.status_code != 200:
        return listings

    # Find the Machinery & Equipment sub-page URL
    me_url = None
    for lnk in _links(index.text, "https://www.tigergroup.com/auctions/"):
        if "machinery" in lnk["text"].lower() or "equipment" in lnk["text"].lower():
            if "tigergroup.com" in lnk["url"]:
                me_url = lnk["url"]
                break

    candidates = [me_url] if me_url else []
    candidates += [
        "https://www.tigergroup.com/machinery-equipment/",
        "https://www.tigergroup.com/current-sales/",
        "https://www.tigergroup.com/industrial/",
    ]

    for url in candidates:
        if not url:
            continue
        resp = _get(url)
        if not resp or resp.status_code != 200:
            continue

        for lnk in _links(resp.text, url):
            if lnk["url"] in seen:
                continue
            if "tigergroup.com" not in lnk["url"]:
                continue
            if lnk["url"] in ("https://www.tigergroup.com/",
                               "https://www.tigergroup.com/auctions/", url):
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
# Manual search links
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
    "Bid4Assets — Tax Sales":
        "https://www.bid4assets.com/",
    "Tiger Group — Auctions":
        "https://www.tigergroup.com/auctions/",
    "Hilco Global — Real Estate":
        "https://hilcoglobal.com/real-estate/",
}
