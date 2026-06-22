#!/usr/bin/env python3
"""Generate the Girl Walk app-health-report static page from live Mixpanel data.

Reads Mixpanel service-account credentials from the environment and writes
index.html. Designed to run in GitHub Actions (cron + manual dispatch) or locally.

Env vars (required):
  MIXPANEL_USERNAME    Service-account username
  MIXPANEL_SECRET      Service-account secret
  MIXPANEL_PROJECT_ID  Numeric project id (Girl Walk = 3972949)
Env vars (optional):
  MIXPANEL_API_BASE    Default https://mixpanel.com/api  (use https://eu.mixpanel.com/api for EU residency)
  REPORT_WINDOW_DAYS   Rolling window length in days (default 7)

Each metric is wrapped in try/except so one failing query degrades only its
section ("no data") instead of failing the whole page — mirrors the skill.
"""

import base64
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

PROJECT_ID = os.environ.get("MIXPANEL_PROJECT_ID", "3972949")
API_BASE = os.environ.get("MIXPANEL_API_BASE", "https://mixpanel.com/api").rstrip("/")
WINDOW_DAYS = int(os.environ.get("REPORT_WINDOW_DAYS", "7"))

USERNAME = os.environ.get("MIXPANEL_USERNAME")
SECRET = os.environ.get("MIXPANEL_SECRET")
if not USERNAME or not SECRET:
    sys.exit("ERROR: set MIXPANEL_USERNAME and MIXPANEL_SECRET (see README).")

_AUTH = base64.b64encode(f"{USERNAME}:{SECRET}".encode()).decode()


def _request(path, params=None, body=None):
    """Call a Mixpanel Query API endpoint. GET if no body, POST (form) otherwise."""
    params = dict(params or {})
    params["project_id"] = PROJECT_ID
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    data = None
    headers = {"Authorization": f"Basic {_AUTH}", "Accept": "application/json"}
    if body is not None:
        data = urllib.parse.urlencode(body).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def jql(script, params):
    """Run a JQL query. Returns the parsed JSON array."""
    return _request("/2.0/jql", body={"script": script, "params": json.dumps(params)})


def unique_users(events, from_date, to_date):
    """Distinct users per event over [from_date, to_date] (range-deduplicated via JQL)."""
    script = """
    function main() {
      return Events({
        from_date: params.from_date, to_date: params.to_date,
        event_selectors: params.events.map(function(e){ return {event: e}; })
      })
      .groupByUser(["name"], mixpanel.reducer.noop())
      .groupBy(["key.1"], mixpanel.reducer.count());
    }
    """
    rows = jql(script, {"from_date": from_date, "to_date": to_date, "events": events})
    out = {e: 0 for e in events}
    for r in rows:
        name = r["key"][0] if isinstance(r.get("key"), list) else r.get("key")
        out[name] = r.get("value", 0)
    return out


def completion_buckets(from_date, to_date):
    """Distribution of `Workout Exited` events by completion_percentage bucket."""
    script = """
    function main() {
      return Events({
        from_date: params.from_date, to_date: params.to_date,
        event_selectors: [{event: "Workout Exited"}]
      })
      .groupBy([function(e){
        var p = Number(e.properties["completion_percentage"]) || 0;
        if (p < 20) return "0-20%";
        if (p < 40) return "20-40%";
        if (p < 60) return "40-60%";
        if (p < 80) return "60-80%";
        if (p < 100) return "80-100%";
        return ">=100%";
      }], mixpanel.reducer.count());
    }
    """
    rows = jql(script, {"from_date": from_date, "to_date": to_date})
    order = ["0-20%", "20-40%", "40-60%", "60-80%", "80-100%", ">=100%"]
    buckets = {b: 0 for b in order}
    for r in rows:
        k = r["key"][0] if isinstance(r.get("key"), list) else r.get("key")
        if k in buckets:
            buckets[k] = r.get("value", 0)
    return buckets, order


def retention(born_event, return_event, from_date, to_date, unit="day"):
    """Cohort retention via the Query API. Returns the raw JSON for parsing."""
    params = {
        "from_date": from_date, "to_date": to_date,
        "retention_type": "birth", "born_event": born_event, "event": return_event,
        "unit": unit, "interval_count": 8,
    }
    return _request("/2.0/retention", params=params)


def avg_retention_rates(ret_json):
    """Average D0..D7 retention fraction across cohorts in a /retention response."""
    if not ret_json:
        return []
    cohorts = list(ret_json.values())
    max_len = max((len(c.get("counts", [])) for c in cohorts), default=0)
    rates = []
    for i in range(max_len):
        num = den = 0
        for c in cohorts:
            counts = c.get("counts", [])
            first = c.get("first", 0)
            if first and i < len(counts):
                num += counts[i]
                den += first
        rates.append(round(num / den, 3) if den else None)
    return rates


def pct(n, d):
    return f"{round(100 * n / d, 1)}%" if d else "—"


def safe(fn, default):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - per-metric graceful degradation
        print(f"  ! metric failed: {e}", file=sys.stderr)
        return default


def main():
    today = dt.date.today()
    to_date = (today - dt.timedelta(days=1)).isoformat()  # yesterday = last full day
    from_date = (today - dt.timedelta(days=WINDOW_DAYS)).isoformat()
    print(f"Window {from_date} -> {to_date} (project {PROJECT_ID})")

    engagement_events = [
        "home_screen_viewed", "workouts_tab_selected", "progress_tab_selected",
        "community_tab_selected", "challenges_tab_selected", "Workout Finished",
        "community_post_opened", "medal_tapped",
    ]
    paywall_events = [
        "SuperWall Paywall Viewed", "SW_subscription_started",
        "SuperWall Paywall Dismissed", "subscription_renewed", "Trial Converted",
        "onboarding_completed",
    ]

    eng = safe(lambda: unique_users(engagement_events, from_date, to_date), {})
    pay = safe(lambda: unique_users(paywall_events, from_date, to_date), {})
    buckets, border = safe(lambda: completion_buckets(from_date, to_date), ({}, []))
    home_ret = safe(lambda: avg_retention_rates(
        retention("home_screen_viewed", "home_screen_viewed", from_date, to_date)), [])
    install_ret = safe(lambda: avg_retention_rates(
        retention("app_installed", "home_screen_viewed", from_date, to_date)), [])

    html = render(from_date, to_date, eng, pay, buckets, border, home_ret, install_ret)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Wrote index.html")


# --- rendering -------------------------------------------------------------

def _ret_cell(rates, idx):
    if rates and idx < len(rates) and rates[idx] is not None:
        return f"{round(rates[idx] * 100)}%"
    return "—"


def render(from_date, to_date, eng, pay, buckets, border, home_ret, install_ret):
    viewed = pay.get("SuperWall Paywall Viewed", 0)
    started = pay.get("SW_subscription_started", 0)
    dismissed = pay.get("SuperWall Paywall Dismissed", 0)
    conv = pct(started, viewed)
    dism = pct(dismissed, viewed)
    total_exits = sum(buckets.values()) if buckets else 0
    low_exits = buckets.get("0-20%", 0)
    low_exit_pct = pct(low_exits, total_exits)

    eng_rows = "".join(
        f"<tr><td>{k}</td><td>{v:,}</td></tr>"
        for k, v in sorted(eng.items(), key=lambda kv: -kv[1])
    ) or '<tr><td colspan="2" class="nodata">No data</td></tr>'

    bucket_rows = "".join(
        f"<tr><td>{b}</td><td>{buckets.get(b, 0):,}</td><td>{pct(buckets.get(b,0), total_exits)}</td></tr>"
        for b in border
    ) or '<tr><td colspan="3" class="nodata">No data</td></tr>'

    gen = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    return TEMPLATE.format(
        from_date=from_date, to_date=to_date, gen=gen,
        home_d1=_ret_cell(home_ret, 1), home_d2=_ret_cell(home_ret, 2),
        home_d7=_ret_cell(home_ret, 7),
        ins_d1=_ret_cell(install_ret, 1), ins_d2=_ret_cell(install_ret, 2),
        ins_d7=_ret_cell(install_ret, 7),
        viewed=f"{viewed:,}", started=f"{started:,}", dismissed=f"{dismissed:,}",
        conv=conv, dism=dism,
        renewed=f"{pay.get('subscription_renewed', 0):,}",
        trial=f"{pay.get('Trial Converted', 0):,}",
        onboarding=f"{pay.get('onboarding_completed', 0):,}",
        eng_rows=eng_rows, bucket_rows=bucket_rows,
        low_exit_pct=low_exit_pct, total_exits=total_exits,
    )


TEMPLATE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Girl Walk — App Health Report</title>
<style>
  :root {{ --bg:#0f1117; --card:#1a1d27; --fg:#f5f6fa; --muted:#9aa0ad;
          --good:#3ecf8e; --warn:#f5a623; --bad:#ff5c5c; --accent:#7c5cff; }}
  * {{ box-sizing:border-box; }} body {{ margin:0; }}
  .wrap {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
          background:var(--bg); color:var(--fg); padding:24px; }}
  .container {{ max-width:1100px; margin:0 auto; }}
  h1 {{ font-size:28px; margin:0 0 4px; }}
  .sub {{ color:var(--muted); margin-bottom:24px; }}
  .verdict {{ display:inline-block; padding:6px 14px; border-radius:999px; font-weight:600;
             background:rgba(62,207,142,.15); color:var(--good); }}
  .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); }}
  .card {{ background:var(--card); border-radius:14px; padding:18px; }}
  .card h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:0 0 12px; }}
  .big {{ font-size:32px; font-weight:700; }}
  .delta.down{{color:var(--bad);}} .delta.up{{color:var(--good);}}
  .scroll {{ overflow-x:auto; }}
  table {{ width:100%; border-collapse:collapse; font-size:14px; }}
  th,td {{ text-align:left; padding:8px 10px; border-bottom:1px solid #262a36; }}
  .nodata {{ color:var(--muted); font-style:italic; }}
  section {{ margin:28px 0; }} code {{ background:#262a36; padding:1px 5px; border-radius:4px; font-size:12px; }}
</style></head><body>
<div class="wrap"><div class="container">
  <h1>Girl Walk — App Health Report</h1>
  <div class="sub">Data window: {from_date} → {to_date} · source: Mixpanel project 3972949 · auto-generated {gen}</div>

  <section><span class="verdict">Auto-refreshed from live Mixpanel</span>
    <div class="grid" style="margin-top:16px">
      <div class="card"><h2>Engaged D1</h2><div class="big">{home_d1}</div><div class="sub">home_screen_viewed self-retention</div></div>
      <div class="card"><h2>Paywall → sub</h2><div class="big">{conv}</div><div class="sub">{started} of {viewed} viewers</div></div>
      <div class="card"><h2>Onboarding completes</h2><div class="big">{onboarding}</div><div class="sub">over the window</div></div>
      <div class="card"><h2>Workout exits &lt;20%</h2><div class="big">{low_exit_pct}</div><div class="sub">{total_exits} total exits</div></div>
    </div>
  </section>

  <section><div class="card"><h2>Engaged-User Retention (home_screen_viewed)</h2>
    <div class="scroll"><table><tr><th>Metric</th><th>D1</th><th>D2</th><th>D7</th></tr>
    <tr><td>$average self-retention</td><td>{home_d1}</td><td>{home_d2}</td><td>{home_d7}</td></tr></table></div>
    <p class="sub" style="margin-top:10px">Recent cohorts have incomplete D7 — read directionally.</p></div></section>

  <section><div class="card"><h2>New vs Returning (install cohort)</h2>
    <div class="scroll"><table><tr><th>Cohort</th><th>D1</th><th>D2</th><th>D7</th></tr>
    <tr><td>New installs (app_installed → home)</td><td>{ins_d1}</td><td>{ins_d2}</td><td>{ins_d7}</td></tr>
    <tr><td>Returning / existing base</td><td colspan="3" class="nodata">not isolable via Query API (needs a saved cohort)</td></tr></table></div></div></section>

  <section><div class="card"><h2>Superwall &amp; Subscription Funnel</h2>
    <div class="scroll"><table><tr><th>Step</th><th>Unique users</th><th>Rate</th></tr>
    <tr><td>SuperWall Paywall Viewed</td><td>{viewed}</td><td>—</td></tr>
    <tr><td>SW_subscription_started</td><td>{started}</td><td class="delta up">{conv}</td></tr>
    <tr><td>SuperWall Paywall Dismissed</td><td>{dismissed}</td><td class="delta down">{dism}</td></tr>
    <tr><td>subscription_renewed</td><td>{renewed}</td><td>—</td></tr>
    <tr><td>Trial Converted</td><td>{trial}</td><td>—</td></tr></table></div></div></section>

  <section><div class="card"><h2>What Users Like (unique users)</h2>
    <div class="scroll"><table><tr><th>Feature</th><th>Unique users</th></tr>{eng_rows}</table></div></div></section>

  <section><div class="card"><h2>Friction — Workout Exit by Completion %</h2>
    <div class="scroll"><table><tr><th>Completion bucket</th><th>Exits</th><th>Share</th></tr>{bucket_rows}</table></div>
    <p class="sub" style="margin-top:10px">High share in the lowest bucket = users quitting near workout start.</p></div></section>

  <p class="sub" style="margin-top:32px;font-size:12px">Auto-generated by GitHub Actions from live Mixpanel. Last day is partial; recent retention cohorts incomplete.</p>
</div></div></body></html>"""


if __name__ == "__main__":
    main()
