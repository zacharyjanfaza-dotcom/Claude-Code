#!/usr/bin/env python3
"""
WSV Sourcing Agent

Usage:
  python main.py               # send weekly digest
  python main.py --dry-run     # save digest_preview.html, no email
  python main.py --diagnose    # print raw scraper diagnostics, no email
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import anthropic

from scraper import (
    scrape_courtlistener,
    scrape_tenx,
    scrape_bid4assets,
    scrape_hilco,
    scrape_tiger,
    scrape_heritage,
    scrape_rabin,
    MANUAL_SEARCH_LINKS,
)
from analyzer import analyze_listings
from emailer import send_digest, build_html

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SCRAPERS = [
    ("CourtListener",    scrape_courtlistener),
    ("Ten-X Commercial", scrape_tenx),
    ("Bid4Assets",       scrape_bid4assets),
    ("Hilco Global",     scrape_hilco),
    ("Tiger Group",      scrape_tiger),
    ("Heritage Global",  scrape_heritage),
    ("Rabin Worldwide",  scrape_rabin),
]


def _safe(name: str, fn) -> tuple[list, str]:
    try:
        results = fn()
        return results, f"{len(results)} found"
    except Exception as exc:
        log.error("%s crashed: %s", name, exc)
        return [], f"error: {str(exc)[:120]}"


def run_diagnose() -> None:
    """Print raw diagnostics for every scraper so we know what's working."""
    import requests as req
    from bs4 import BeautifulSoup as BS

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }

    # 1. CourtListener API probe
    print("\n═══ CourtListener API ═══")
    for term in ["manufacturing", "industrial"]:
        try:
            r = req.get(
                "https://www.courtlistener.com/api/rest/v4/dockets/",
                params={"q": term, "page_size": 5, "order_by": "date_filed desc"},
                headers={**headers, "Accept": "application/json"},
                timeout=15,
            )
            data = r.json()
            count = data.get("count", "?")
            results = data.get("results") or []
            print(f"  q={term!r} → HTTP {r.status_code}, count={count}, "
                  f"results_in_page={len(results)}")
            for res in results[:2]:
                print(f"    • {res.get('case_name') or res.get('caseName', '(no name)')}")
        except Exception as exc:
            print(f"  q={term!r} → ERROR: {exc}")

    # 2. HTML site probes
    sites = [
        ("Hilco Global",    "https://hilcoglobal.com/service/industrial/"),
        ("Tiger Group",     "https://www.tigergroup.com/auctions/"),
        ("Heritage Global", "https://www.hgp.com/auctions"),
        ("Rabin Worldwide", "https://www.rabinworldwide.com/auctions/"),
        ("Ten-X",           "https://www.ten-x.com/listings/"),
        ("Bid4Assets",      "https://www.bid4assets.com/real-estate"),
    ]
    for name, url in sites:
        print(f"\n═══ {name} ({url}) ═══")
        try:
            r = req.get(url, headers=headers, timeout=20)
            soup = BS(r.text, "lxml")
            all_links = soup.find_all("a", href=True)
            print(f"  HTTP {r.status_code} · {len(r.content):,} bytes · "
                  f"{len(all_links)} <a> tags")
            # Show first 5 non-trivial link texts
            shown = 0
            for lnk in all_links:
                txt = lnk.get_text(" ", strip=True)
                if txt and len(txt) > 10:
                    print(f"    link: {txt[:80]!r}")
                    shown += 1
                    if shown >= 5:
                        break
        except Exception as exc:
            print(f"  ERROR: {exc}")

    print("\n═══ Done ═══\n")


def main(dry_run: bool = False, diagnose: bool = False) -> None:
    if diagnose:
        run_diagnose()
        return

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")
    digest_to = os.environ.get("DIGEST_TO")

    if not anthropic_key:
        sys.exit("ERROR: ANTHROPIC_API_KEY not set")
    if not dry_run and not (gmail_address and gmail_password and digest_to):
        sys.exit("ERROR: GMAIL_ADDRESS, GMAIL_APP_PASSWORD, DIGEST_TO must be set")

    client = anthropic.Anthropic(api_key=anthropic_key)

    # ── 1. Scrape ──────────────────────────────
    all_listings = []
    scraper_status = {}

    for name, fn in SCRAPERS:
        log.info("Scraping %s…", name)
        results, status = _safe(name, fn)
        all_listings += results
        scraper_status[name] = status

    log.info("Total collected: %d listings", len(all_listings))

    # ── 2. Analyze ─────────────────────────────
    log.info("Analyzing with Claude…")
    analysis = analyze_listings(all_listings, client)
    analysis["_scraper_status"] = scraper_status
    analysis["_manual_links"] = MANUAL_SEARCH_LINKS

    # ── 3. Deliver ─────────────────────────────
    if dry_run:
        out = Path(__file__).parent / "digest_preview.html"
        out.write_text(build_html(analysis))
        log.info("Dry run saved to %s", out)
        print(f"\nSummary: {analysis.get('summary', '')}")
        print(f"Industrial: {len(analysis.get('top_industrial', []))}  "
              f"Bankruptcy: {len(analysis.get('top_bankruptcy', []))}")
    else:
        send_digest(
            analysis=analysis,
            gmail_address=gmail_address,
            gmail_app_password=gmail_password,
            recipient=digest_to,
        )
        log.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--diagnose", action="store_true",
                        help="Print raw scraper diagnostics without sending email")
    args = parser.parse_args()
    main(dry_run=args.dry_run, diagnose=args.diagnose)
