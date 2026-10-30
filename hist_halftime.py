#!/usr/bin/env python3
"""
Halftime-window check for The Odds API history (~71 credits).
Pulls snapshots at +40, +45, ... +70 minutes after tip-off (7 snapshots, 10 credits each) for several
games that tipped at the same time, and shows when each game's line stops moving (the halftime freeze).

  export ODDS_API_KEY=your_key
  python3 hist_halftime.py                       # 5 games, 2026-02-07
  python3 hist_halftime.py --date 2026-01-17 --games 6

Output: a table per game (median home spread, # books, '=' when unchanged from the previous snapshot),
the longest unchanged stretch, and probe_out/hist_halftime.csv with every book's number.
"""
import argparse, csv, json, os, statistics, sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

BASE = os.environ.get("ODDS_API_BASE", "https://api.the-odds-api.com/v4")
SPORT = "basketball_ncaab"
KEY = os.environ.get("ODDS_API_KEY") or sys.exit("set ODDS_API_KEY first")
OUT = Path("probe_out"); OUT.mkdir(exist_ok=True)
OFFS = list(range(40, 75, 5))
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))


def call(path, **params):
    r = requests.get(f"{BASE}{path}", params=dict(apiKey=KEY, **params), timeout=60)
    print(f"  {path.split('/')[-1]:<7} HTTP {r.status_code}  credits remaining {r.headers.get('x-requests-remaining')}")
    if r.status_code != 200:
        sys.exit(f"stopped: {r.text[:300]}")
    return r.json()


def book_spreads(ev):
    d = {}
    for b in ev.get("bookmakers", []):
        for m in b.get("markets", []):
            if m["key"] == "spreads":
                for o in m["outcomes"]:
                    if o["name"] == ev["home_team"]:
                        d[b["key"]] = o["point"]
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-02-07"); ap.add_argument("--games", type=int, default=5)
    a = ap.parse_args()
    day = datetime.fromisoformat(a.date).replace(tzinfo=timezone.utc)
    print("finding games (1 credit)")
    ev = call(f"/historical/sports/{SPORT}/events", date=iso(day + timedelta(hours=18)))
    cand = [e for e in ev["data"] if day + timedelta(hours=21) <= parse(e["commence_time"]) <= day + timedelta(hours=27)]
    if not cand:
        sys.exit("no evening games that date: try another Saturday")
    tip, _ = Counter(e["commence_time"] for e in cand).most_common(1)[0]
    pick = [e for e in cand if e["commence_time"] == tip][:a.games]
    t0 = parse(tip)
    print(f"tip-off {tip}: " + "; ".join(f"{e['away_team']} @ {e['home_team']}" for e in pick))

    print(f"pulling {len(OFFS)} snapshots")
    data = {e["id"]: {} for e in pick}
    for off in OFFS:
        snap = call(f"/historical/sports/{SPORT}/odds", date=iso(t0 + timedelta(minutes=off)), regions="us", markets="spreads", oddsFormat="american")
        (OUT / f"hh_snapshot_{off}.json").write_text(json.dumps(snap))
        byid = {e["id"]: e for e in snap.get("data", [])}
        for e in pick:
            data[e["id"]][off] = (snap.get("timestamp"), book_spreads(byid[e["id"]]) if e["id"] in byid else None)

    rows = []
    for e in pick:
        print(f"\n{e['away_team']} @ {e['home_team']}   (home spread; = means unchanged since previous snapshot)")
        prev, run, best = None, [], []
        for off in OFFS:
            ts, bs = data[e["id"]][off]
            if not bs:
                print(f"  +{off:2d}  {ts}   not in snapshot"); prev = None; run = []; continue
            med = statistics.median(bs.values())
            same = prev is not None and abs(med - prev) < 0.01
            run = run + [off] if same else [off]
            best = run if len(run) > len(best) else best
            print(f"  +{off:2d}  {ts}   {med:+6.2f}  ({len(bs)} books: {min(bs.values()):+.1f} to {max(bs.values()):+.1f})  {'=' if same else ''}")
            for k, v in bs.items():
                rows.append(dict(game=f"{e['away_team']} @ {e['home_team']}", offset_min=off, snapshot=ts, book=k, home_spread=v))
            prev = med
        if len(best) > 1:
            print(f"  -> longest unchanged stretch: +{best[0]} to +{best[-1]} min ({len(best)} snapshots) - likely the halftime freeze")
        else:
            print("  -> no unchanged stretch at 5-minute spacing")
    with open(OUT / "hist_halftime.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["game", "offset_min", "snapshot", "book", "home_spread"]); w.writeheader(); w.writerows(rows)
    print("\nsaved probe_out/hist_halftime.csv and raw snapshots")


main()
