# Girl Walk — App Health Report

Public dashboard of Girl Walk app health, auto-generated from live Mixpanel data.

**Live page:** https://ziadali22.github.io/girlwalk-app-health/

## How it refreshes

`scripts/generate_report.py` queries the Mixpanel Query API and rewrites `index.html`.
The GitHub Action `.github/workflows/refresh.yml` runs it:

- **On a schedule** — daily at 06:00 UTC.
- **On demand** — go to the **Actions** tab → **Refresh app health report** → **Run workflow**.

The Mixpanel secret lives only in GitHub Secrets; it is never written into the public page.

The page shows the latest release plus a **3-version comparison** and a **first-time
home_screen_viewed** table. Release windows are defined in `releases.json`.

## One-time setup

1. **Fill in release dates** in `releases.json` — replace each `REPLACE-ME` with the
   App Store release date (`YYYY-MM-DD`). The script builds each version's window as
   `[date, day-before-next-release]` and compares the newest `compare_count` (default 3).

2. **Create a Mixpanel service account:** Mixpanel → project → Settings → Service Accounts →
   Add Service Account (Analyst/Consumer role). Copy the username and secret.

2. **Add the repo secrets** (run locally; values stay on your machine):
   ```bash
   gh secret set MIXPANEL_USERNAME   --repo ziadali22/girlwalk-app-health
   gh secret set MIXPANEL_SECRET     --repo ziadali22/girlwalk-app-health
   gh secret set MIXPANEL_PROJECT_ID --repo ziadali22/girlwalk-app-health --body 3972949
   ```
   (Each prompts for the value; paste it at the prompt.)

3. **Run it once:** Actions tab → Run workflow (or `gh workflow run refresh.yml --repo ziadali22/girlwalk-app-health`).

## Local testing

```bash
set -a; source ~/.mixpanel-girlwalk.env; set +a
python3 scripts/generate_report.py   # rewrites index.html
```

`~/.mixpanel-girlwalk.env` (git-ignored, never committed):
```
MIXPANEL_USERNAME=...
MIXPANEL_SECRET=...
MIXPANEL_PROJECT_ID=3972949
```

If your Mixpanel project is on EU data residency, also set
`MIXPANEL_API_BASE=https://eu.mixpanel.com/api`.
