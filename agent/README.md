# WSV Sourcing Agent

Scrapes shallow bay industrial listings from Crexi and bankruptcy/liquidation
listings for manufacturing facilities, then emails a deal-flow digest.

## Sources
| Source | What it pulls |
|---|---|
| **Crexi** | Industrial listings, East Coast states |
| **BankruptcyData.com** | Chapter 7 / 11 filings in manufacturing & industrial sectors |
| **Hilco Industrial** | Liquidation auctions for industrial facilities |
| **Tiger Group** | Industrial liquidation auctions |

Claude (Opus 4.7) reviews every listing and selects the top opportunities that match WSV's thesis.

## Setup

### 1. Install dependencies
```bash
cd agent
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
```

Edit `.env` and fill in:
- `ANTHROPIC_API_KEY` — from console.anthropic.com
- `GMAIL_ADDRESS` — your sending Gmail/Google Workspace address
- `GMAIL_APP_PASSWORD` — **App Password** (not your login password)
  - Google Account → Security → 2-Step Verification → App passwords → Create
- `DIGEST_TO` — where the digest should land (e.g. `zachary@walkerstreetventures.com`)

### 3. Run
```bash
# Preview (saves digest_preview.html, no email sent)
python main.py --dry-run

# Send email
python main.py
```

## Schedule (run weekly, Monday 7am)

**Mac / Linux — crontab:**
```
0 7 * * 1 /path/to/agent/.venv/bin/python /path/to/agent/main.py >> /tmp/wsv-agent.log 2>&1
```
Open crontab editor: `crontab -e`

**GitHub Actions (run in the cloud, free):**
Create `.github/workflows/sourcing-agent.yml`:
```yaml
name: WSV Sourcing Agent
on:
  schedule:
    - cron: "0 12 * * 1"   # Mondays at 12pm UTC (8am ET)
  workflow_dispatch:         # manual trigger button in GitHub UI

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r agent/requirements.txt
      - run: python agent/main.py
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          GMAIL_ADDRESS: ${{ secrets.GMAIL_ADDRESS }}
          GMAIL_APP_PASSWORD: ${{ secrets.GMAIL_APP_PASSWORD }}
          DIGEST_TO: ${{ secrets.DIGEST_TO }}
```
Add the four secrets in GitHub → Settings → Secrets and variables → Actions.
