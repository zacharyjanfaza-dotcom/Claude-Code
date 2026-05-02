"""
Scrapes two sources:
  1. Crexi – industrial property listings on the East Coast
  2. Bankruptcy/liquidation listings for manufacturing facilities
     (BankruptcyData.com + Hilco Industrial + Tiger Group auctions)
"""

import re
import json
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

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

EAST_COAST_STATES = ["NY", "NJ", "CT", "MA", "RI", "NH", "ME", "VT",
                     "PA", "MD", "DE", "VA", "NC", "SC", "GA", "FL", "DC"]


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
# Crexi
# ─────────────────────────────────────────────

CREXI_SEARCH_URL = "https://www.crexi.com/properties"

CREXI_API_URL = "https://api.crexi.com/assets"

CREXI_PARAMS = {
    "types": "Industrial",
    "states": ",".join(EAST_COAST_STATES),
    "limit": 50,
    "offset": 0,
    "sort": "PublishedDate",
    "sortDirection": "Descending",
}

CREXI_API_PARAMS = {
    "propertyTypes": "Industrial",
    "states": EAST_COAST_STATES,
    "pageSize": 50,
    "page": 1,
    "sortBy": "dateAdded",
    "sortDir": "desc",
}


def _crexi_api(session: requests.Session, page: int = 1) -> list[Listing]:
    """Try Crexi's internal REST API (reverse-engineered from their SPA)."""
    params = {**CREXI_API_PARAMS, "page": page}
    try:
        resp = session.get(
            CREXI_API_URL,
            params=params,
            headers={**HEADERS, "Accept": "application/json", "Origin": "https://www.crexi.com"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        assets = data.get("assets") or data.get("results") or data.get("data") or []
        listings = []
        for a in assets:
            listings.append(Listing(
                source="Crexi",
                title=a.get("name") or a.get("title") or "Industrial Property",
                url=f"https://www.crexi.com/properties/{a.get('id') or a.get('slug', '')}",
                location=_join(a.get("city"), a.get("state")),
                price=_fmt_price(a.get("askingPrice") or a.get("price")),
                size_sf=_fmt_sf(a.get("buildingSize") or a.get("totalSqFt")),
                property_type=a.get("propertyType") or "Industrial",
                description=a.get("description") or a.get("summary") or "",
                raw=a,
            ))
        return listings
    except Exception as exc:
        log.debug("Crexi API attempt failed: %s", exc)
        return []


def _crexi_html(session: requests.Session) -> list[Listing]:
    """Fall back to scraping the public search page."""
    try:
        resp = session.get(
            CREXI_SEARCH_URL,
            params={
                "types": "Industrial",
                "states": ",".join(EAST_COAST_STATES),
            },
            headers=HEADERS,
            timeout=25,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Many SPAs embed their initial Redux/Zustand store as JSON in a <script> tag
        for script in soup.find_all("script"):
            text = script.string or ""
            if "assets" in text and "propertyType" in text:
                match = re.search(r'"assets"\s*:\s*(\[.*?\])', text, re.DOTALL)
                if match:
                    try:
                        assets = json.loads(match.group(1))
                        return [
                            Listing(
                                source="Crexi",
                                title=a.get("name") or "Industrial Property",
                                url=f"https://www.crexi.com/properties/{a.get('id', '')}",
                                location=_join(a.get("city"), a.get("state")),
                                price=_fmt_price(a.get("askingPrice")),
                                size_sf=_fmt_sf(a.get("buildingSize")),
                                property_type="Industrial",
                                description=a.get("description") or "",
                                raw=a,
                            )
                            for a in assets
                        ]
                    except json.JSONDecodeError:
                        pass

        # Last resort: parse visible listing cards
        listings = []
        for card in soup.select("[data-qa='property-card'], .property-card, .asset-card"):
            title = _text(card, "h2, h3, .title, .name")
            loc = _text(card, ".location, .address, .city-state")
            price = _text(card, ".price, .asking-price")
            sf = _text(card, ".size, .sqft, .building-size")
            href = card.find("a", href=True)
            url = ("https://www.crexi.com" + href["href"]) if href else CREXI_SEARCH_URL
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
        return listings
    except Exception as exc:
        log.warning("Crexi HTML scrape failed: %s", exc)
        return []


def scrape_crexi() -> list[Listing]:
    session = requests.Session()
    listings = _crexi_api(session)
    if not listings:
        log.info("Crexi API returned nothing, falling back to HTML scrape")
        listings = _crexi_html(session)
    log.info("Crexi: found %d listings", len(listings))
    return listings


# ─────────────────────────────────────────────
# BankruptcyData.com – recent Chapter 7 / 11 filings
# ─────────────────────────────────────────────

BKDATA_URL = "https://www.bankruptcydata.com/research/recent-filings"

# SIC ranges 2000-3999 broadly cover manufacturing
MFG_KEYWORDS = [
    "manufactur", "industrial", "fabricat", "processing", "warehouse",
    "distribution", "assembly", "production", "plant", "mill",
]


def scrape_bankruptcydata() -> list[Listing]:
    session = requests.Session()
    listings: list[Listing] = []
    try:
        resp = session.get(BKDATA_URL, headers=HEADERS, timeout=25)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        table = soup.find("table")
        if not table:
            return listings

        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            company = cells[0].get_text(strip=True)
            industry = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            state = cells[2].get_text(strip=True) if len(cells) > 2 else ""
            chapter = cells[3].get_text(strip=True) if len(cells) > 3 else ""
            date = cells[4].get_text(strip=True) if len(cells) > 4 else ""

            if state not in EAST_COAST_STATES:
                continue

            text_blob = (company + " " + industry).lower()
            if not any(kw in text_blob for kw in MFG_KEYWORDS):
                continue

            link = row.find("a", href=True)
            url = ("https://www.bankruptcydata.com" + link["href"]) if link else BKDATA_URL

            listings.append(Listing(
                source="BankruptcyData.com",
                title=f"{company} — Chapter {chapter} ({date})",
                url=url,
                location=state,
                property_type="Manufacturing / Industrial (Bankruptcy)",
                description=f"Industry: {industry}. Chapter {chapter} filed {date}.",
            ))

    except Exception as exc:
        log.warning("BankruptcyData scrape failed: %s", exc)

    log.info("BankruptcyData: found %d relevant filings", len(listings))
    return listings


# ─────────────────────────────────────────────
# Hilco Industrial – liquidation auctions
# ─────────────────────────────────────────────

HILCO_URL = "https://www.hilcoind.com/auctions/"


def scrape_hilco() -> list[Listing]:
    session = requests.Session()
    listings: list[Listing] = []
    try:
        resp = session.get(HILCO_URL, headers=HEADERS, timeout=25)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        for item in soup.select(".auction-item, .event-item, article.post, .listing"):
            title = _text(item, "h2, h3, .title, .event-title")
            loc = _text(item, ".location, .address, .meta-location")
            date = _text(item, ".date, .auction-date, time")
            link = item.find("a", href=True)
            url = link["href"] if link and link["href"].startswith("http") else (
                "https://www.hilcoind.com" + (link["href"] if link else "")
            )
            desc = _text(item, "p, .excerpt, .description")

            if title:
                text_blob = (title + " " + desc).lower()
                if any(kw in text_blob for kw in MFG_KEYWORDS):
                    listings.append(Listing(
                        source="Hilco Industrial",
                        title=title,
                        url=url,
                        location=loc,
                        property_type="Industrial Liquidation Auction",
                        description=f"{date} — {desc}".strip(" —"),
                    ))

    except Exception as exc:
        log.warning("Hilco scrape failed: %s", exc)

    log.info("Hilco: found %d listings", len(listings))
    return listings


# ─────────────────────────────────────────────
# Tiger Group – industrial auction listings
# ─────────────────────────────────────────────

TIGER_URL = "https://www.tigergroup.com/auctions/"


def scrape_tiger() -> list[Listing]:
    session = requests.Session()
    listings: list[Listing] = []
    try:
        resp = session.get(TIGER_URL, headers=HEADERS, timeout=25)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        for item in soup.select(".auction, .listing-item, article, .event"):
            title = _text(item, "h2, h3, .auction-title, .title")
            loc = _text(item, ".location, .state, .city")
            date = _text(item, ".date, time, .auction-date")
            desc = _text(item, "p, .excerpt, .description")
            link = item.find("a", href=True)
            url = link["href"] if link and link["href"].startswith("http") else (
                "https://www.tigergroup.com" + (link["href"] if link else "")
            )

            if not title:
                continue
            text_blob = (title + " " + desc + " " + loc).lower()
            if not any(kw in text_blob for kw in MFG_KEYWORDS):
                continue

            listings.append(Listing(
                source="Tiger Group",
                title=title,
                url=url,
                location=loc,
                property_type="Industrial Liquidation Auction",
                description=f"{date} — {desc}".strip(" —"),
            ))

    except Exception as exc:
        log.warning("Tiger Group scrape failed: %s", exc)

    log.info("Tiger Group: found %d listings", len(listings))
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
            return f"${n/1_000_000:.2f}M"
        if n >= 1_000:
            return f"${n/1_000:.0f}K"
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
