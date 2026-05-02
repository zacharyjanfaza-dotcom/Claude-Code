"""
Sends the weekly deal-flow digest via Gmail SMTP.
Requires a Gmail App Password (not your regular password).
"""

import logging
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)


def _listing_rows(listings: list[dict], cols: list[str]) -> str:
    rows = ""
    for l in listings:
        rows += "<tr>"
        for col in cols:
            val = l.get(col, "")
            if col == "title" and l.get("url"):
                val = f'<a href="{l["url"]}" style="color:#0d1b2a;font-weight:600;">{val}</a>'
            rows += f'<td style="padding:10px 12px;border-bottom:1px solid #e8e8e8;vertical-align:top;">{val}</td>'
        rows += "</tr>"
        # Rationale row
        if l.get("rationale"):
            span = len(cols)
            rows += (
                f'<tr><td colspan="{span}" style="padding:4px 12px 14px;'
                f'font-size:0.82em;color:#666;border-bottom:1px solid #e8e8e8;">'
                f'<em>{l["rationale"]}</em></td></tr>'
            )
    return rows


def build_html(analysis: dict) -> str:
    run_date = date.today().strftime("%B %d, %Y")

    top_industrial = analysis.get("top_industrial", [])
    top_bankruptcy = analysis.get("top_bankruptcy", [])
    summary = analysis.get("summary", "")

    industrial_section = ""
    if top_industrial:
        rows = _listing_rows(
            top_industrial,
            ["title", "location", "price", "size_sf"],
        )
        industrial_section = f"""
        <h2 style="font-family:Georgia,serif;color:#0d1b2a;font-size:1.1rem;
                   border-bottom:2px solid #c9a84c;padding-bottom:8px;margin-top:32px;">
          Shallow Bay Industrial ({len(top_industrial)} listings)
        </h2>
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;font-size:0.9rem;color:#1c1c1c;">
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

    bankruptcy_section = ""
    if top_bankruptcy:
        rows = _listing_rows(top_bankruptcy, ["title", "location"])
        bankruptcy_section = f"""
        <h2 style="font-family:Georgia,serif;color:#0d1b2a;font-size:1.1rem;
                   border-bottom:2px solid #c9a84c;padding-bottom:8px;margin-top:32px;">
          Bankruptcy &amp; Liquidation Opportunities ({len(top_bankruptcy)} filings)
        </h2>
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;font-size:0.9rem;color:#1c1c1c;">
          <thead>
            <tr style="background:#f4f4f4;text-align:left;">
              <th style="padding:10px 12px;">Company / Filing</th>
              <th style="padding:10px 12px;">State</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>"""

    no_results = ""
    if not top_industrial and not top_bankruptcy:
        no_results = "<p style='color:#666;'>No qualifying opportunities found this run.</p>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f8f5ef;font-family:'Inter',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8f5ef;padding:32px 0;">
    <tr><td align="center">
      <table width="640" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:12px;overflow:hidden;
                    box-shadow:0 4px 24px rgba(0,0,0,0.08);">

        <!-- Header -->
        <tr>
          <td style="background:#0d1b2a;padding:28px 36px;">
            <span style="background:#c9a84c;color:#0d1b2a;font-family:Georgia,serif;
                         font-weight:700;font-size:0.75rem;letter-spacing:0.05em;
                         padding:5px 9px;border-radius:4px;">WSV</span>
            <span style="color:#ffffff;font-family:Georgia,serif;font-size:1rem;
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

            <p style="margin-top:40px;font-size:0.78rem;color:#aaa;border-top:1px solid #e8e8e8;
                      padding-top:16px;">
              Generated by the WSV sourcing agent. Always verify listings directly before acting.
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def send_digest(
    analysis: dict,
    gmail_address: str,
    gmail_app_password: str,
    recipient: str,
) -> None:
    run_date = date.today().strftime("%B %d, %Y")
    subject = f"WSV Deal Flow Digest — {run_date}"

    html_body = build_html(analysis)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"WSV Sourcing Agent <{gmail_address}>"
    msg["To"] = recipient

    n_ind = len(analysis.get("top_industrial", []))
    n_bk = len(analysis.get("top_bankruptcy", []))
    plain = (
        f"WSV Deal Flow Digest — {run_date}\n\n"
        f"{analysis.get('summary', '')}\n\n"
        f"Industrial listings: {n_ind}\n"
        f"Bankruptcy/liquidation: {n_bk}\n\n"
        "View the HTML version for full details."
    )
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    log.info("Sending digest to %s via %s", recipient, gmail_address)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, recipient, msg.as_string())
    log.info("Digest sent successfully")
