#!/usr/bin/env python3
"""
WSV Sourcing Agent — weekly deal-flow digest.

Usage:
  python main.py             # send email
  python main.py --dry-run   # save digest_preview.html, no email
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


def _safe(name: str, fn) -> tuple[list, str]:
    try:
        results = fn()
        return results, f"{len(results)} found"
    except Exception as exc:
        log.error("%s crashed: %s", name, exc)
        return [], f"error: {str(exc)[:80]}"


def main(dry_run: bool = False) -> None:
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

    scrapers = [
        ("CourtListener",   scrape_courtlistener),
        ("Ten-X Commercial", scrape_tenx),
        ("Bid4Assets",      scrape_bid4assets),
        ("Hilco Global",    scrape_hilco),
        ("Tiger Group",     scrape_tiger),
        ("Heritage Global", scrape_heritage),
        ("Rabin Worldwide", scrape_rabin),
    ]

    for name, fn in scrapers:
        log.info("Scraping %s…", name)
        results, status = _safe(name, fn)
        all_listings += results
        scraper_status[name] = status

    log.info("Total collected: %d listings", len(all_listings))

    # ── 2. Analyze with Claude ──────────────────
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
    main(dry_run=parser.parse_args().dry_run)
