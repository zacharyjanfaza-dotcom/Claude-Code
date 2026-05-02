#!/usr/bin/env python3
"""
WSV Sourcing Agent
Scrapes shallow bay industrial listings (Crexi) and bankruptcy/liquidation
listings for manufacturing facilities, then emails a digest.

Usage:
  python main.py                  # run now
  python main.py --dry-run        # print digest HTML, don't send email
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import anthropic

from scraper import scrape_crexi, scrape_bankruptcydata, scrape_hilco, scrape_tiger
from analyzer import analyze_listings
from emailer import send_digest, build_html

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def main(dry_run: bool = False) -> None:
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")
    digest_to = os.environ.get("DIGEST_TO")

    if not anthropic_key:
        sys.exit("ERROR: ANTHROPIC_API_KEY not set in .env")
    if not dry_run and not (gmail_address and gmail_password and digest_to):
        sys.exit("ERROR: GMAIL_ADDRESS, GMAIL_APP_PASSWORD, and DIGEST_TO must be set in .env")

    client = anthropic.Anthropic(api_key=anthropic_key)

    # ── 1. Scrape ─────────────────────────────
    log.info("Starting scrape run")
    all_listings = []

    log.info("Scraping Crexi…")
    all_listings += scrape_crexi()

    log.info("Scraping BankruptcyData.com…")
    all_listings += scrape_bankruptcydata()

    log.info("Scraping Hilco Industrial…")
    all_listings += scrape_hilco()

    log.info("Scraping Tiger Group…")
    all_listings += scrape_tiger()

    log.info("Total raw listings collected: %d", len(all_listings))

    # ── 2. Analyze with Claude ─────────────────
    log.info("Analyzing with Claude (%s)…", "claude-opus-4-7")
    analysis = analyze_listings(all_listings, client)

    # ── 3. Send / preview ─────────────────────
    if dry_run:
        out_path = Path(__file__).parent / "digest_preview.html"
        out_path.write_text(build_html(analysis))
        log.info("Dry run: digest saved to %s", out_path)
        print(f"\nSummary:\n{analysis.get('summary', '')}")
        print(f"\nTop industrial listings: {len(analysis.get('top_industrial', []))}")
        print(f"Top bankruptcy listings: {len(analysis.get('top_bankruptcy', []))}")
    else:
        send_digest(
            analysis=analysis,
            gmail_address=gmail_address,
            gmail_app_password=gmail_password,
            recipient=digest_to,
        )
        log.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WSV Sourcing Agent")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Save digest to HTML file instead of sending email",
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run)
