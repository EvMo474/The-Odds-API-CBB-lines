#!/usr/bin/env python3
"""
Historical-odds test for The Odds API (needs a paid key).  Costs ~60 credits.

Question: do the historical snapshots contain LIVE (in-game) lines, or only pregame lines?
It picks 3 games that tipped at the same time on a past date, then pulls snapshots just before
tip-off and at +45/+55/+65/+75/+95 minutes (halftime is usually ~+50 to +70), and prints the spread
and total from each snapshot.

  export ODDS_API_KEY=your_key
  python3 hist_test.py                 # default date 2026-02-07
  python3 hist_test.py --date 2026-01-17

How to read it: if the SPREAD moves away from the pregame number once the games are underway (live
full-game lines keep the full-game total, so the total stays near its pregame level), the history has live
lines. If everything is identical to pregame, or the games vanish once they start, it does not.
"""
import argparse, json, os, statistics, sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

BASE = os.environ.get("ODDS_API_BASE", "https://api.the-odds-api.com/v4")
SPORT = "basketball_ncaab"
KEY = os.environ.get("ODDS_API_KEY") or sys.exit("set ODDS_API_KEY first")
OUT = Path("probe_out"); OUT.mkdir(exist_ok=True)
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))


def call(path, **params):
    r = requests.get(f"{BASE}{path}", params=dict(apiKey=KEY, **params), timeout=60)
    print(f"  {path.split('/')[-1]:<8} -> HTTP {r.status_code}   credits used {r.headers.get('x-requests-used')}  remaining {r.headers.get('x-requests-remaining')}")
    if r.status_code != 200:
        sys.exit(f"stopped: {r.text[:300]}")
    return r.json()


def summarize(ev):
    sp, tot = [], []
    for b in ev.get("bookmakers", []):
        for m in b.get("markets", []):
            if m["key"] == "spreads":
                for o in m["outcomes"]:
                    if o["name"] == ev["home_team"]:
                        sp.append(o["point"])
            if m["key"] == "totals":
                for o in m["outcomes"]:
                    if o["name"] == "Over":
                        tot.append(o["point"])
    f = lambda xs: (f"{statistics.median(xs):+.1f} (range {min(xs):+.1f} to {max(xs):+.1f}, {len(xs)} books)" if xs else "none")
    return (statistics.median(sp) if sp else None), (statistics.median(tot) if tot else None), f(sp), f(tot)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--date", default="2026-02-07"); a = ap.parse_args()
    day = datetime.fromisoformat(a.date).replace(tzinfo=timezone.utc)
    print("1) finding games (1 credit)")
    ev = call(f"/historical/sports/{SPORT}/events", date=iso(day + timedelta(hours=18)))
    (OUT / "hist_events.json").write_text(json.dumps(ev, indent=1))
    games = [e for e in ev["data"] if day + timedelta(hours=21) <= parse(e["commence_time"]) <= day + timedelta(hours=27)]
    if not games:
        sys.exit("no evening games found for that date: try --date with a regular-season Saturday")
    tip, _ = Counter(e["commence_time"] for e in games).most_common(1)[0]
    pick = [e for e in games if e["commence_time"] == tip][:3]
    t0 = parse(tip)
    print(f"   tip-off {tip}: testing", "; ".join(f"{e['away_team']} @ {e['home_team']}" for e in pick))

    print("2) pulling snapshots (10 credits each)")
    rows = {e["id"]: [] for e in pick}
    for off in (-10, 45, 55, 65, 75, 95):
        snap = call(f"/historical/sports/{SPORT}/odds", date=iso(t0 + timedelta(minutes=off)), regions="us",
                    markets="spreads,totals", oddsFormat="american")
        (OUT / f"hist_snapshot_{off:+d}.json").write_text(json.dumps(snap, indent=1))
        byid = {e["id"]: e for e in snap.get("data", [])}
        for e in pick:
            rows[e["id"]].append((off, snap.get("timestamp"), byid.get(e["id"])))

    print("\n=== RESULTS (home-team spread, negative = home favored) ===")
    live_flags = []
    for e in pick:
        print(f"\n{e['away_team']} @ {e['home_team']}")
        pre_tot = pre_sp = None
        for off, ts, ev_ in rows[e["id"]]:
            if ev_ is None:
                print(f"  {off:+4d} min  [{ts}]  game NOT in snapshot"); live_flags.append(False if off > 0 else None); continue
            s, t, sf, tf = summarize(ev_)
            if off < 0: pre_tot, pre_sp = t, s
            print(f"  {off:+4d} min  [{ts}]  spread {sf} | total {tf}")
            if off in (45, 55, 65, 75):
                live_flags.append(pre_sp is not None and s is not None and abs(s - pre_sp) >= 0.5)
    print("\n=== VERDICT ===")
    f = [x for x in live_flags if x is not None]
    if f and sum(f) >= len(f) / 2:
        print("Spreads move once the games start => snapshots contain LIVE lines. The historical download is worth doing.")
    elif f:
        print("Lines look unchanged during the game (or games disappear) => probably PRE-GAME ONLY. Not worth the full pull; send me probe_out/ to double-check.")
    else:
        print("Inconclusive: send me probe_out/")
    print("Raw snapshots saved in probe_out/")


main()
