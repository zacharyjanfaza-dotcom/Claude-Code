"""
Builds and sends the weekly WSV deal-flow digest via Gmail SMTP.
"""

import logging
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)


# ── Table helpers ──────────────────────────────────────────────────────

def _listing_rows(listings: list[dict], cols: list[str]) -> str:
    rows = ""
    for l in listings:
        rows += "<tr>"
        for col in cols:
            val = l.get(col) or ""
            if col == "title" and l.get("url"):
                val = f'<a href="{l["url"]}" style="color:#0d1b2a;font-weight:600;">{val}</a>'
            rows += (
                f'<td style="padding:10px 12px;border-bottom:1px solid #e8e8e8;'
                f'vertical-align:top;">{val}</td>'
            )
        rows += "</tr>"
        if l.get("rationale"):
            span = len(cols)
            rows += (
                f'<tr><td colspan="{span}" style="padding:4px 12px 14px;font-size:0.82em;'
                f'color:#666;border-bottom:1px solid #e8e8e8;">'
                f'<em>{l["rationale"]}</em></td></tr>'
            )
    return rows


# ── Manual search link buttons ─────────────────────────────────────────

def _search_buttons(links: dict) -> str:
    if not links:
        return ""
    buttons = ""
    for label, url in links.items():
        buttons += (
            f'<a href="{url}" style="display:inline-block;margin:4px 6px 4px 0;'
            f'padding:8px 14px;background:#f4f4f4;border:1px solid #e8e8e8;'
            f'border-radius:6px;font-size:0.78rem;font-weight:600;color:#0d1b2a;'
            f'text-decoration:none;">{label} →</a>'
        )
    return f"""
    <div style="margin-top:32px;">
      <p style="font-family:Georgia,serif;font-size:0.9rem;font-weight:700;
                color:#0d1b2a;border-bottom:2px solid #c9a84c;padding-bottom:8px;
                margin-bottom:12px;">Quick Searches</p>
      <p style="font-size:0.8rem;color:#888;margin-bottom:10px;">
        These sites require a browser — click to open a pre-filtered search.
      </p>
      {buttons}
    </div>"""


# ── Scraper status footer ──────────────────────────────────────────────

def _status_footer(scraper_status: dict) -> str:
    if not scraper_status:
        return ""
    rows = "".join(
        f"<span style='margin-right:16px;'><strong>{k}:</strong> {v}</span>"
        for k, v in scraper_status.items()
    )
    return f"<br><br>Source status: {rows}"


# ── HTML builder ───────────────────────────────────────────────────────

def build_html(analysis: dict) -> str:
    run_date = date.today().strftime("%B %d, %Y")

    top_industrial = analysis.get("top_industrial") or []
    top_bankruptcy = analysis.get("top_bankruptcy") or []
    summary = analysis.get("summary") or ""
    manual_links = analysis.get("_manual_links") or {}
    scraper_status = analysis.get("_scraper_status") or {}

    # Industrial table
    industrial_section = ""
    if top_industrial:
        rows = _listing_rows(top_industrial, ["title", "location", "price", "size_sf"])
        industrial_section = f"""
        <h2 style="font-family:Georgia,serif;color:#0d1b2a;font-size:1.05rem;
                   border-bottom:2px solid #c9a84c;padding-bottom:8px;margin-top:32px;">
          Shallow Bay Industrial — {len(top_industrial)} listings
        </h2>
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;font-size:0.88rem;color:#1c1c1c;">
          <thead>
            <tr style="background:#f4f4f4;text-align:left;">
              <th style="padding:10px 12px;">Property</th>
              <th style="padding:10px 12px;">Location</th>
              <th style="padding:10px 12px;">Price</th>
              <th style="padding:10px 12px;">Size</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>"""

    # Bankruptcy / liquidation table
    bankruptcy_section = ""
    if top_bankruptcy:
        rows = _listing_rows(top_bankruptcy, ["title", "location", "property_type"])
        bankruptcy_section = f"""
        <h2 style="font-family:Georgia,serif;color:#0d1b2a;font-size:1.05rem;
                   border-bottom:2px solid #c9a84c;padding-bottom:8px;margin-top:32px;">
          Bankruptcy &amp; Liquidation — {len(top_bankruptcy)} opportunities
        </h2>
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;font-size:0.88rem;color:#1c1c1c;">
          <thead>
            <tr style="background:#f4f4f4;text-align:left;">
              <th style="padding:10px 12px;">Company / Asset</th>
              <th style="padding:10px 12px;">Location</th>
              <th style="padding:10px 12px;">Type</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>"""

    no_results = ""
    if not top_industrial and not top_bankruptcy:
        no_results = """
        <p style="color:#666;padding:20px 0;">
          No qualifying opportunities were found by automated scrapers this run.
          Use the quick search links below to check manually.
        </p>"""

    search_section = _search_buttons(manual_links)
    footer_status = _status_footer(scraper_status)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f8f5ef;font-family:'Inter',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8f5ef;padding:32px 0;">
    <tr><td align="center">
      <table width="640" cellpadding="0" cellspacing="0"
             style="background:#fff;border-radius:12px;overflow:hidden;
                    box-shadow:0 4px 24px rgba(0,0,0,0.08);">

        <!-- Header -->
        <tr>
          <td style="background:#0d1b2a;padding:28px 36px;">
            <span style="background:#c9a84c;color:#0d1b2a;font-family:Georgia,serif;
                         font-weight:700;font-size:0.75rem;letter-spacing:0.05em;
                         padding:5px 9px;border-radius:4px;">WSV</span>
            <span style="color:#fff;font-family:Georgia,serif;font-size:1rem;
                         font-weight:600;margin-left:10px;">Walker Street Ventures</span>
            <p style="color:rgba(255,255,255,0.5);font-size:0.78rem;margin:8px 0 0;">
              Deal Flow Digest — {run_date}
            </p>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:32px 36px;">

            {"" if not summary else f'<p style="color:#4a4a4a;line-height:1.65;margin:0 0 8px;">{summary}</p>'}

            {industrial_section}
            {bankruptcy_section}
            {no_results}
            {search_section}

            <p style="margin-top:36px;font-size:0.72rem;color:#aaa;
                      border-top:1px solid #e8e8e8;padding-top:16px;line-height:1.7;">
              Generated by the WSV sourcing agent. Always verify listings directly.
              {footer_status}
            </p>

          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ── Send ───────────────────────────────────────────────────────────────

def send_digest(
    analysis: dict,
    gmail_address: str,
    gmail_app_password: str,
    recipient: str,
) -> None:
    run_date = date.today().strftime("%B %d, %Y")
    subject = f"WSV Deal Flow Digest — {run_date}"
    html_body = build_html(analysis)

    n_ind = len(analysis.get("top_industrial") or [])
    n_bk  = len(analysis.get("top_bankruptcy") or [])
    plain = (
        f"WSV Deal Flow Digest — {run_date}\n\n"
        f"{analysis.get('summary', '')}\n\n"
        f"Industrial listings: {n_ind}\n"
        f"Bankruptcy/liquidation: {n_bk}\n\n"
        "View the HTML version for full details and search links."
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"WSV Sourcing Agent <{gmail_address}>"
    msg["To"]      = recipient
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    log.info("Sending to %s…", recipient)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, recipient, msg.as_string())
    log.info("Sent.")
