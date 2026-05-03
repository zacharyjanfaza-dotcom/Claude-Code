"""
Uses Claude to analyze raw listings and identify the most actionable opportunities.
"""

import json
import logging
import re
from dataclasses import asdict

import anthropic

from scraper import Listing

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-7"


def analyze_listings(
    listings: list[Listing],
    client: anthropic.Anthropic,
) -> dict:
    """
    Sends all listings to Claude, which scores and filters them.
    Returns a dict with:
      - "top_industrial": list of dicts (shallow bay industrial)
      - "top_bankruptcy": list of dicts (bankruptcy/liquidation)
      - "summary": str  (executive summary paragraph)
    """
    if not listings:
        return {"top_industrial": [], "top_bankruptcy": [], "summary": "No listings found this run."}

    # Serialize listings to a compact JSON payload
    payload = []
    for i, l in enumerate(listings):
        payload.append({
            "id": i,
            "source": l.source,
            "title": l.title,
            "url": l.url,
            "location": l.location,
            "price": l.price,
            "size_sf": l.size_sf,
            "type": l.property_type,
            "description": l.description[:400],  # cap to keep tokens manageable
        })

    system_prompt = """You are an analyst for Walker Street Ventures, a real estate investment firm
that acquires value-add properties along the East Coast of the United States. You are looking
for off-market or lightly marketed deals where institutional competition is minimal.

INVESTMENT CRITERIA (ALL must apply):
- East Coast states only: MA, RI, CT, NY, NJ, PA, MD, DE, VA, NC, SC, GA, FL, DC
- Deal size: sub $5 million (price, auction estimate, or asset value)
- Asset types: (1) shallow bay industrial / flex / light manufacturing, (2) unanchored strip retail,
  (3) manufacturing or industrial facilities available through bankruptcy/liquidation
- Strongly prefer off-market, distressed, tax sale, foreclosure, or liquidation situations
- Avoid: broadly marketed listings with major broker (CBRE, JLL, Cushman, Eastdil, Newmark)
  acting as exclusive sell-side advisor, institutional portfolio trades, and any deal where
  a large PE firm or REIT is the likely buyer

SCORING GUIDANCE:
- Highest priority: bankruptcy liquidations, tax deed auctions, receivership sales, sheriff sales,
  motivated private sellers — these have less competition from deep-pocketed buyers
- Medium priority: small-broker or direct-seller listings under $5M, value-add retail or industrial
  with below-market rents or deferred maintenance
- Exclude: anything that reads like a national marketing campaign, trophy assets, ground leases,
  new construction, or deals clearly above $5M

For each listing you select, write a 1-2 sentence rationale explaining specifically why it fits
(distress angle, size, location, lack of institutional interest, etc.)."""

    user_prompt = f"""Here are {len(payload)} sourced listings from today's run.
Please analyze them and return a JSON object with these exact keys:
{{
  "top_industrial": [
    {{
      "id": <original id>,
      "title": "...",
      "url": "...",
      "location": "...",
      "price": "...",
      "size_sf": "...",
      "rationale": "..."
    }}
  ],
  "top_bankruptcy": [
    {{
      "id": <original id>,
      "title": "...",
      "url": "...",
      "location": "...",
      "rationale": "..."
    }}
  ],
  "summary": "A 2-3 sentence executive summary of this week's deal flow."
}}

Select up to 8 industrial/retail listings and up to 5 bankruptcy/liquidation opportunities that
best fit the criteria above. Be selective — only include listings that are genuinely actionable
(right size, right geography, minimal institutional competition). If fewer than that qualify,
include only those that truly fit. Do not pad the list.

Listings:
{json.dumps(payload, indent=2)}"""

    log.info("Sending %d listings to Claude for analysis", len(payload))

    response = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        thinking={"type": "adaptive"},
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    # Extract the text block (thinking blocks may be present too)
    text = ""
    for block in response.content:
        if block.type == "text":
            text = block.text
            break

    # Parse JSON from the response
    try:
        # Handle markdown code fences if present
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-z]*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)
        result = json.loads(cleaned)
        log.info(
            "Claude selected %d industrial and %d bankruptcy listings",
            len(result.get("top_industrial", [])),
            len(result.get("top_bankruptcy", [])),
        )
        return result
    except (json.JSONDecodeError, ValueError) as exc:
        log.error("Failed to parse Claude response as JSON: %s\n%s", exc, text[:500])
        return {
            "top_industrial": [],
            "top_bankruptcy": [],
            "summary": "Analysis completed but response parsing failed. See logs.",
        }


