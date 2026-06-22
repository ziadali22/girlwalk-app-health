#!/usr/bin/env python3
"""Generate the Girl Walk app-health-report static page from live Mixpanel data.

Reads Mixpanel service-account credentials + release dates and writes index.html.
Runs in GitHub Actions (cron + manual dispatch) or locally.

Env vars (required):
  MIXPANEL_USERNAME    Service-account username
  MIXPANEL_SECRET      Service-account secret
  MIXPANEL_PROJECT_ID  Numeric project id (Girl Walk = 3972949)
Env vars (optional):
  MIXPANEL_API_BASE    Default https://mixpanel.com/api (use https://eu.mixpanel.com/api for EU)

Release windows come from releases.json (App Store dates). The newest N versions
(compare_count) are shown side by side; the latest version's window also drives the
detailed engagement/funnel/friction sections.

Every query is wrapped in safe() so one failure degrades only its cell/section.
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

USERNAME = os.environ.get("MIXPANEL_USERNAME")
SECRET = os.environ.get("MIXPANEL_SECRET")
if not USERNAME or not SECRET:
    sys.exit("ERROR: set MIXPANEL_USERNAME and MIXPANEL_SECRET (see README).")

_AUTH = base64.b64encode(f"{USERNAME}:{SECRET}".encode()).decode()
TODAY = dt.date.today()
YESTERDAY = (TODAY - dt.timedelta(days=1)).isoformat()  # last full day


# --- Mixpanel Query API ----------------------------------------------------

def _request(path, params=None, body=None):
    params = dict(params or {})
    params["project_id"] = PROJECT_ID
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    headers = {"Authorization": f"Basic {_AUTH}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = urllib.parse.urlencode(body).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode())


def jql(script, params):
    return _request("/2.0/jql", body={"script": script, "params": json.dumps(params)})


def unique_users(events, frm, to):
    """Range-deduplicated distinct users per event over [frm, to] via JQL."""
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
    rows = jql(script, {"from_date": frm, "to_date": to, "events": events})
    out = {e: 0 for e in events}
    for r in rows:
        name = r["key"][0] if isinstance(r.get("key"), list) else r.get("key")
        if name in out:
            out[name] = r.get("value", 0)
    return out


def daily_unique_avg(event, frm, to):
    """Average daily unique users of an event over the window (segmentation API)."""
    res = _request("/2.0/segmentation", params={
        "event": event, "from_date": frm, "to_date": to, "type": "unique", "unit": "day"})
    series = res.get("data", {}).get("values", {}).get(event, {})
    vals = list(series.values())
    return round(sum(vals) / len(vals)) if vals else 0


def completion_buckets(frm, to):
    script = """
    function main() {
      return Events({
        from_date: params.from_date, to_date: params.to_date,
        event_selectors: [{event: "Workout Exited"}]
      })
      .groupBy([function(e){
        var p = Number(e.properties["completion_percentage"]) || 0;
        if (p < 20) return "0-20%"; if (p < 40) return "20-40%";
        if (p < 60) return "40-60%"; if (p < 80) return "60-80%";
        if (p < 100) return "80-100%"; return ">=100%";
      }], mixpanel.reducer.count());
    }
    """
    rows = jql(script, {"from_date": frm, "to_date": to})
    order = ["0-20%", "20-40%", "40-60%", "60-80%", "80-100%", ">=100%"]
    buckets = {b: 0 for b in order}
    for r in rows:
        k = r["key"][0] if isinstance(r.get("key"), list) else r.get("key")
        if k in buckets:
            buckets[k] = r.get("value", 0)
    return buckets, order


def retention(born_event, return_event, frm, to, unit="day"):
    """Birth-cohort retention. Cohort = users whose FIRST born_event is in the window."""
    return _request("/2.0/retention", params={
        "from_date": frm, "to_date": to,
        "retention_type": "birth", "born_event": born_event, "event": return_event,
        "unit": unit, "interval_count": 8})


def avg_retention_rates(ret_json):
    """Average D0..D7 retention fraction weighted by cohort size."""
    if not ret_json:
        return []
    cohorts = list(ret_json.values())
    max_len = max((len(c.get("counts", [])) for c in cohorts), default=0)
    rates = []
    for i in range(max_len):
        num = den = 0
        for c in cohorts:
            counts, first = c.get("counts", []), c.get("first", 0)
            if first and i < len(counts):
                num += counts[i]
                den += first
        rates.append(round(num / den, 3) if den else None)
    return rates


def cohort_first_total(ret_json):
    """Total first-time born users across all cohorts = new activations in window."""
    if not ret_json:
        return 0
    return sum(c.get("first", 0) for c in ret_json.values())


# --- helpers ---------------------------------------------------------------

def pct(n, d):
    return f"{round(100 * n / d, 1)}%" if d else "—"


def safe(fn, default):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — per-metric graceful degradation
        print(f"  ! metric failed: {e}", file=sys.stderr)
        return default


def ret_cell(rates, idx):
    if rates and idx < len(rates) and rates[idx] is not None:
        return f"{round(rates[idx] * 100)}%"
    return "—"


def load_release_windows():
    """Return [{version, frm, to, complete}] for the newest compare_count versions."""
    with open("releases.json", encoding="utf-8") as f:
        cfg = json.load(f)
    rel = [r for r in cfg["releases"]
           if r.get("date") and "REPLACE" not in r["date"]]
    rel.sort(key=lambda r: r["date"])
    n = int(cfg.get("compare_count", 3))
    windows = []
    for i, r in enumerate(rel):
        start = r["date"]
        if i + 1 < len(rel):
            nxt = dt.date.fromisoformat(rel[i + 1]["date"]) - dt.timedelta(days=1)
            end, complete = nxt.isoformat(), True
        else:
            end, complete = YESTERDAY, False  # current release, still running
        windows.append({"version": r["version"], "frm": start, "to": end,
                         "complete": complete})
    return windows[-n:]


# --- per-version metrics ---------------------------------------------------

def version_metrics(w):
    frm, to = w["frm"], w["to"]
    home_ret = safe(lambda: retention("home_screen_viewed", "home_screen_viewed", frm, to), {})
    rates = avg_retention_rates(home_ret)
    pay = safe(lambda: unique_users(
        ["SuperWall Paywall Viewed", "SW_subscription_started"], frm, to), {})
    return {
        "version": w["version"], "window": f"{frm} → {to}", "complete": w["complete"],
        "avg_day": safe(lambda: daily_unique_avg("home_screen_viewed", frm, to), 0),
        "d1": ret_cell(rates, 1), "d2": ret_cell(rates, 2), "d7": ret_cell(rates, 7),
        "first_home": cohort_first_total(home_ret),
        "conv": pct(pay.get("SW_subscription_started", 0),
                    pay.get("SuperWall Paywall Viewed", 0)),
    }


# --- main ------------------------------------------------------------------

def main():
    windows = safe(load_release_windows, [])
    if not windows:
        sys.exit("ERROR: no usable release dates in releases.json (fill in real dates).")

    per_version = [version_metrics(w) for w in windows]
    latest = windows[-1]
    frm, to = latest["frm"], latest["to"]
    print(f"Latest window {latest['version']}: {frm} -> {to}")

    engagement_events = [
        "home_screen_viewed", "workouts_tab_selected", "progress_tab_selected",
        "community_tab_selected", "challenges_tab_selected", "Workout Finished",
        "community_post_opened", "medal_tapped",
    ]
    funnel_events = [
        "SuperWall Paywall Viewed", "SW_subscription_started",
        "SuperWall Paywall Dismissed", "subscription_renewed", "Trial Converted",
        "onboarding_completed",
    ]
    eng = safe(lambda: unique_users(engagement_events, frm, to), {})
    pay = safe(lambda: unique_users(funnel_events, frm, to), {})
    buckets, border = safe(lambda: completion_buckets(frm, to), ({}, []))

    html = render(latest, per_version, eng, pay, buckets, border)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Wrote index.html")


# --- rendering -------------------------------------------------------------

def render(latest, per_version, eng, pay, buckets, border):
    viewed = pay.get("SuperWall Paywall Viewed", 0)
    started = pay.get("SW_subscription_started", 0)
    dismissed = pay.get("SuperWall Paywall Dismissed", 0)
    conv, dism = pct(started, viewed), pct(dismissed, viewed)
    total_exits = sum(buckets.values()) if buckets else 0
    low_exit_pct = pct(buckets.get("0-20%", 0), total_exits)

    # Version comparison table (newest last)
    cmp_rows = "".join(
        f"<tr><td>{m['version']}{' *' if not m['complete'] else ''}</td>"
        f"<td>{m['window']}</td><td>{m['avg_day']:,}</td>"
        f"<td>{m['d1']}</td><td>{m['d2']}</td><td>{m['d7']}</td>"
        f"<td>{m['conv']}</td></tr>"
        for m in per_version
    ) or '<tr><td colspan="7" class="nodata">No release windows</td></tr>'

    # First-time home_screen_viewed table (new activations per version)
    first_rows = "".join(
        f"<tr><td>{m['version']}{' *' if not m['complete'] else ''}</td>"
        f"<td>{m['first_home']:,}</td></tr>"
        for m in per_version
    ) or '<tr><td colspan="2" class="nodata">No data</td></tr>'

    eng_rows = "".join(
        f"<tr><td>{k}</td><td>{v:,}</td></tr>"
        for k, v in sorted(eng.items(), key=lambda kv: -kv[1])
    ) or '<tr><td colspan="2" class="nodata">No data</td></tr>'

    bucket_rows = "".join(
        f"<tr><td>{b}</td><td>{buckets.get(b, 0):,}</td>"
        f"<td>{pct(buckets.get(b, 0), total_exits)}</td></tr>"
        for b in border
    ) or '<tr><td colspan="3" class="nodata">No data</td></tr>'

    cur = next((m for m in per_version if m["version"] == latest["version"]),
               per_version[-1] if per_version else {"d1": "—", "first_home": 0})
    gen = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    return TEMPLATE.format(
        version=latest["version"], frm=latest["frm"], to=latest["to"], gen=gen,
        cmp_rows=cmp_rows, first_rows=first_rows, eng_rows=eng_rows,
        bucket_rows=bucket_rows,
        cur_d1=cur.get("d1", "—"), cur_first=f"{cur.get('first_home', 0):,}",
        viewed=f"{viewed:,}", started=f"{started:,}", dismissed=f"{dismissed:,}",
        conv=conv, dism=dism, renewed=f"{pay.get('subscription_renewed', 0):,}",
        trial=f"{pay.get('Trial Converted', 0):,}",
        onboarding=f"{pay.get('onboarding_completed', 0):,}",
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
  th,td {{ text-align:left; padding:8px 10px; border-bottom:1px solid #262a36; white-space:nowrap; }}
  .nodata {{ color:var(--muted); font-style:italic; }}
  section {{ margin:28px 0; }} code {{ background:#262a36; padding:1px 5px; border-radius:4px; font-size:12px; }}
</style></head><body>
<div class="wrap"><div class="container">
  <h1>Girl Walk — App Health Report</h1>
  <div class="sub">Latest version {version} · window {frm} → {to} · Mixpanel project 3972949 · auto-generated {gen}</div>

  <section><span class="verdict">Auto-refreshed from live Mixpanel</span>
    <div class="grid" style="margin-top:16px">
      <div class="card"><h2>Engaged D1 ({version})</h2><div class="big">{cur_d1}</div><div class="sub">home self-retention</div></div>
      <div class="card"><h2>Paywall → sub</h2><div class="big">{conv}</div><div class="sub">{started} of {viewed} viewers</div></div>
      <div class="card"><h2>First-time home ({version})</h2><div class="big">{cur_first}</div><div class="sub">new activations</div></div>
      <div class="card"><h2>Workout exits &lt;20%</h2><div class="big">{low_exit_pct}</div><div class="sub">{total_exits} total exits</div></div>
    </div>
  </section>

  <section><div class="card"><h2>Version Comparison (last 3 releases)</h2>
    <div class="scroll"><table>
    <tr><th>Version</th><th>Window</th><th>Avg home/day</th><th>D1</th><th>D2</th><th>D7</th><th>Paywall conv.</th></tr>
    {cmp_rows}</table></div>
    <p class="sub" style="margin-top:10px">D1/D2/D7 = home_screen_viewed birth-cohort retention ($average). * = current release, window still open / D7 incomplete.</p></div></section>

  <section><div class="card"><h2>First-time home_screen_viewed (new activations)</h2>
    <div class="scroll"><table><tr><th>Version</th><th>First-time home viewers</th></tr>{first_rows}</table></div>
    <p class="sub" style="margin-top:10px">Users whose <em>first ever</em> home_screen_viewed fell inside each release window — i.e. newly activated users reaching the home screen.</p></div></section>

  <section><div class="card"><h2>Superwall &amp; Subscription Funnel — {version}</h2>
    <div class="scroll"><table><tr><th>Step</th><th>Unique users</th><th>Rate</th></tr>
    <tr><td>SuperWall Paywall Viewed</td><td>{viewed}</td><td>—</td></tr>
    <tr><td>SW_subscription_started</td><td>{started}</td><td class="delta up">{conv}</td></tr>
    <tr><td>SuperWall Paywall Dismissed</td><td>{dismissed}</td><td class="delta down">{dism}</td></tr>
    <tr><td>subscription_renewed</td><td>{renewed}</td><td>—</td></tr>
    <tr><td>Trial Converted</td><td>{trial}</td><td>—</td></tr>
    <tr><td>onboarding_completed</td><td>{onboarding}</td><td>—</td></tr></table></div></div></section>

  <section><div class="card"><h2>What Users Like — {version} (unique users)</h2>
    <div class="scroll"><table><tr><th>Feature</th><th>Unique users</th></tr>{eng_rows}</table></div></div></section>

  <section><div class="card"><h2>Friction — Workout Exit by Completion % — {version}</h2>
    <div class="scroll"><table><tr><th>Completion bucket</th><th>Exits</th><th>Share</th></tr>{bucket_rows}</table></div>
    <p class="sub" style="margin-top:10px">High share in the lowest bucket = users quitting near workout start.</p></div></section>

  <p class="sub" style="margin-top:32px;font-size:12px">Auto-generated by GitHub Actions from live Mixpanel. Last day partial; current release D7 incomplete.</p>
</div></div></body></html>"""


if __name__ == "__main__":
    main()
