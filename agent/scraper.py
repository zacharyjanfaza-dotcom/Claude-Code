"""
Scrapes two source categories:
  1. Crexi – industrial property listings on the East Coast
  2. Bankruptcy/liquidation listings for manufacturing facilities
     (CourtListener PACER feed + Hilco Global + Tiger Group)
"""

import json
import logging
import re
import urllib.parse
from dataclasses import dataclass, field

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
# Crexi  (tries REST API first, then HTML)
# ─────────────────────────────────────────────

# Crexi's internal search API — discovered from network traffic on their SPA
_CREXI_API_CANDIDATES = [
    "https://api.crexi.com/assets",
    "https://www.crexi.com/api/assets",
]

_CREXI_API_PARAMS = {
    "propertyTypes[]": "Industrial",
    "states[]": EAST_COAST_STATES,
    "limit": 50,
    "offset": 0,
    "sortBy": "dateAdded",
    "sortDir": "desc",
}


def _crexi_via_api(session: requests.Session) -> list[Listing]:
    for base_url in _CREXI_API_CANDIDATES:
        try:
            resp = session.get(
                base_url,
                params=_CREXI_API_PARAMS,
                headers={**HEADERS, "Accept": "application/json",
                         "Referer": "https://www.crexi.com/"},
                timeout=20,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            assets = (
                data.get("assets")
                or data.get("results")
                or data.get("data")
                or []
            )
            if not assets:
                continue
            return [
                Listing(
                    source="Crexi",
                    title=a.get("name") or a.get("title") or "Industrial Property",
                    url=f"https://www.crexi.com/properties/{a.get('id') or a.get('slug', '')}",
                    location=_join(a.get("city"), a.get("state")),
                    price=_fmt_price(a.get("askingPrice") or a.get("price")),
                    size_sf=_fmt_sf(a.get("buildingSize") or a.get("totalSqFt")),
                    property_type=a.get("propertyType") or "Industrial",
                    description=(a.get("description") or a.get("summary") or "")[:400],
                    raw=a,
                )
                for a in assets
            ]
        except Exception as exc:
            log.debug("Crexi API candidate %s failed: %s", base_url, exc)
    return []


def _crexi_via_html(session: requests.Session) -> list[Listing]:
    """
    Crexi is a React SPA — plain HTML requests usually return an empty shell.
    We look for an embedded __NEXT_DATA__ or window.__INITIAL_STATE__ JSON blob
    that Next.js / React apps often include for SSR hydration.
    """
    try:
        resp = session.get(
            "https://www.crexi.com/properties",
            params={"types": "Industrial", "states": ",".join(EAST_COAST_STATES)},
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Next.js hydration blob
        next_data = soup.find("script", id="__NEXT_DATA__")
        if next_data and next_data.string:
            try:
                blob = json.loads(next_data.string)
                # Path varies by Crexi's page structure — walk common keys
                assets = (
                    _deep_get(blob, "props", "pageProps", "assets")
                    or _deep_get(blob, "props", "pageProps", "listings")
                    or []
                )
                if assets:
                    return [
                        Listing(
                            source="Crexi",
                            title=a.get("name") or "Industrial Property",
                            url=f"https://www.crexi.com/properties/{a.get('id', '')}",
                            location=_join(a.get("city"), a.get("state")),
                            price=_fmt_price(a.get("askingPrice")),
                            size_sf=_fmt_sf(a.get("buildingSize")),
                            property_type="Industrial",
                            description=(a.get("description") or "")[:400],
                        )
                        for a in assets
                    ]
            except (json.JSONDecodeError, TypeError):
                pass

        # Generic JSON blob scan
        for script in soup.find_all("script"):
            text = script.string or ""
            if '"propertyType"' in text and '"Industrial"' in text:
                m = re.search(r'"assets"\s*:\s*(\[.{20,}?\])', text, re.DOTALL)
                if m:
                    try:
                        assets = json.loads(m.group(1))
                        return [
                            Listing(
                                source="Crexi",
                                title=a.get("name") or "Industrial Property",
                                url=f"https://www.crexi.com/properties/{a.get('id', '')}",
                                location=_join(a.get("city"), a.get("state")),
                                price=_fmt_price(a.get("askingPrice")),
                                size_sf=_fmt_sf(a.get("buildingSize")),
                                property_type="Industrial",
                            )
                            for a in assets
                        ]
                    except json.JSONDecodeError:
                        pass

    except Exception as exc:
        log.warning("Crexi HTML scrape failed: %s", exc)
    return []


def scrape_crexi() -> list[Listing]:
    session = requests.Session()
    listings = _crexi_via_api(session)
    if not listings:
        log.info("Crexi API returned nothing, trying HTML scrape")
        listings = _crexi_via_html(session)
    if not listings:
        log.warning(
            "Crexi returned 0 listings — their site requires JavaScript rendering. "
            "Consider adding Playwright (pip install playwright) for full support."
        )
    log.info("Crexi: %d listings", len(listings))
    return listings


# ─────────────────────────────────────────────
# CourtListener — public PACER bankruptcy search
# Free API, no key required for basic search
# ─────────────────────────────────────────────

_CL_API = "https://www.courtlistener.com/api/rest/v4/dockets/"

_EC_COURT_SLUGS = [
    "nyed", "nysd", "nynd", "nywd",        # New York
    "njd",                                   # New Jersey
    "ctd",                                   # Connecticut
    "mad",                                   # Massachusetts
    "paed", "pawd", "pamd",                 # Pennsylvania
    "mdd",                                   # Maryland
    "vaed", "vawd",                          # Virginia
    "nced", "ncwd", "ncmd",                 # North Carolina
    "sced", "scnd",                          # South Carolina
    "gaed", "gawd", "gamd", "gandce",       # Georgia (ndga etc)
    "flsd", "flnd", "flmd",                 # Florida
    "ded",                                   # Delaware
]


def scrape_courtlistener() -> list[Listing]:
    """
    Searches CourtListener's free public API for recent Chapter 7/11 bankruptcy
    dockets in East Coast federal courts that mention manufacturing keywords.
    """
    session = requests.Session()
    listings: list[Listing] = []

    for keyword in ["manufacturing", "industrial facility", "fabrication", "plant"]:
        try:
            resp = session.get(
                _CL_API,
                params={
                    "q": keyword,
                    "type": "r",
                    "order_by": "date_filed desc",
                    "nature_of_suit": "830",  # bankruptcy
                    "filed_after": _days_ago(90),
                    "page_size": 20,
                },
                headers={**HEADERS, "Accept": "application/json"},
                timeout=20,
            )
            if resp.status_code != 200:
                continue
            results = resp.json().get("results") or []
            for r in results:
                court = r.get("court_id") or r.get("court") or ""
                # Filter to East Coast courts where possible
                case_name = r.get("case_name") or r.get("caseName") or ""
                date_filed = r.get("date_filed") or ""
                docket_number = r.get("docket_number") or ""
                url = r.get("absolute_url") or ""
                if url and not url.startswith("http"):
                    url = "https://www.courtlistener.com" + url

                listings.append(Listing(
                    source="CourtListener (PACER)",
                    title=f"{case_name} ({docket_number})",
                    url=url or "https://www.courtlistener.com/",
                    location=str(court).upper(),
                    property_type="Bankruptcy Filing",
                    description=f"Filed {date_filed}. Search term: {keyword}.",
                ))
        except Exception as exc:
            log.debug("CourtListener keyword '%s' failed: %s", keyword, exc)

    # Deduplicate by URL
    seen = set()
    unique = []
    for l in listings:
        if l.url not in seen:
            seen.add(l.url)
            unique.append(l)

    log.info("CourtListener: %d unique filings", len(unique))
    return unique


# ─────────────────────────────────────────────
# Hilco Global — industrial liquidations
# ─────────────────────────────────────────────

_HILCO_URLS = [
    "https://hilcoglobal.com/service/industrial/",
    "https://hilcoind.com/auctions/",          # may have SSL issues
    "https://www.hilco.com/auctions/",
]


def scrape_hilco() -> list[Listing]:
    session = requests.Session()
    listings: list[Listing] = []

    for url in _HILCO_URLS:
        try:
            resp = session.get(
                url,
                headers=HEADERS,
                timeout=25,
                verify=True,
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            for item in soup.select(
                ".auction-item, .event-item, article.post, "
                ".listing, .service-item, .project-item, li.item"
            ):
                title = _text(item, "h2, h3, h4, .title, .event-title, .name")
                if not title:
                    continue
                text_blob = (title + " " + _text(item, "p, .excerpt")).lower()
                if not any(kw in text_blob for kw in MFG_KEYWORDS):
                    continue

                link = item.find("a", href=True)
                href = link["href"] if link else ""
                full_url = href if href.startswith("http") else (
                    urllib.parse.urljoin(url, href) if href else url
                )

                listings.append(Listing(
                    source="Hilco Global",
                    title=title,
                    url=full_url,
                    location=_text(item, ".location, .address, .city"),
                    property_type="Industrial Liquidation",
                    description=_text(item, "p, .excerpt, .description")[:300],
                ))

            if listings:
                break  # stop trying alternate URLs once we get results

        except Exception as exc:
            log.debug("Hilco URL %s failed: %s", url, exc)

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

    for url in _TIGER_URLS:
        try:
            resp = session.get(url, headers=HEADERS, timeout=25)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            for item in soup.select(".auction, .listing-item, article, .event, .sale-item"):
                title = _text(item, "h2, h3, h4, .auction-title, .title")
                if not title:
                    continue
                desc = _text(item, "p, .excerpt, .description")
                loc = _text(item, ".location, .state, .city, .address")
                text_blob = (title + " " + desc + " " + loc).lower()
                if not any(kw in text_blob for kw in MFG_KEYWORDS):
                    continue

                link = item.find("a", href=True)
                href = link["href"] if link else ""
                full_url = href if href.startswith("http") else (
                    urllib.parse.urljoin(url, href) if href else url
                )

                listings.append(Listing(
                    source="Tiger Group",
                    title=title,
                    url=full_url,
                    location=loc,
                    property_type="Industrial Liquidation Auction",
                    description=desc[:300],
                ))

        except Exception as exc:
            log.debug("Tiger URL %s failed: %s", url, exc)

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


def _days_ago(n: int) -> str:
    from datetime import date, timedelta
    return (date.today() - timedelta(days=n)).isoformat()
