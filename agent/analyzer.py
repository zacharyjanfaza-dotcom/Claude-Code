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
that acquires (1) shallow bay industrial properties and (2) unanchored strip retail along the
East Coast of the United States. Your job is to review sourced listings and identify the most
actionable opportunities.

Shallow bay industrial criteria:
- Single-story, multi-tenant or small-bay industrial/flex buildings
- East Coast submarkets (MA, RI, CT, NY, NJ, PA, MD, DE, VA, NC, SC, GA, FL)
- Supply-constrained infill/last-mile locations are preferred
- Typical sizes: 10,000 – 200,000 SF
- Value-add, repositioning, or below-market rents preferred

Bankruptcy/liquidation criteria:
- Manufacturing or industrial facilities that may become available below market
- Chapter 7 liquidations and Chapter 11 reorganizations with real estate components
- East Coast preferred but national bankruptcy filings with EC facilities are relevant

For each listing you select, provide a 1-2 sentence rationale explaining why it fits the thesis."""

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

Select the top 10 industrial listings and top 5 bankruptcy/liquidation opportunities that best fit
the Walker Street Ventures thesis. If fewer qualify, include only those that do.

Listings:
{json.dumps(payload, indent=2)}"""

    log.info("Sending %d listings to Claude for analysis", len(payload))

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
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


