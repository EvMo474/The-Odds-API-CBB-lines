#!/usr/bin/env python3
"""Flatten data/games/*/*.json into data/team_rows.csv (2 rows per game) and print bucket summaries.
Usage: python report.py [--gap 10]
Benchmarks per team-row (line_exp = team's expected margin, + = favorite):
  cover_half : 2H margin beats 0.5 * pregame line_exp                      (score-blind, optimistic)
  cover_live : 2H margin beats (live full-game line_exp at halftime) - halftime margin
               ONLY meaningful if ESPN's halftime odds are a live full-game line (check live_moved).
"""
import argparse, glob, json
import numpy as np, pandas as pd

def first_spread(lst):
    for o in lst or []:
        if o.get("home_spread") is not None:
            return o["home_spread"], o.get("total")
    return None, None

rows = []
for f in sorted(glob.glob("data/games/*/*.json")):
    g = json.load(open(f))
    ht, fin = g.get("halftime"), g.get("final")
    if not ht or not fin or not fin.get("box_full") or fin.get("periods", 2) > 2:
        continue
    pre, _ = first_spread((g.get("pregame") or {}).get("odds"))
    ticks = [t for t in g["odds_ticks"] if t["status"] == "STATUS_HALFTIME"]
    live, _ = first_spread(ticks[-1]["odds"]) if ticks else (None, None)
    if live is None:
        live, _ = first_spread(ht.get("summary_odds"))
    b1, bf = ht["box_1h"], fin["box_full"]
    s1 = {k: int(ht["score"][k]) for k in ("home", "away")}
    sf = {k: int(fin["score"][k]) for k in ("home", "away")}
    for side, opp in (("home", "away"), ("away", "home")):
        if not (b1.get(side, {}).get("fga") and b1.get(opp, {}).get("fga")):
            continue
        sign = 1 if side == "home" else -1                      # home_spread<0 => home favored
        lx = None if pre is None else -pre * sign
        ll = None if live is None else -live * sign
        ht_m = s1[side] - s1[opp]
        h2 = (sf[side] - s1[side]) - (sf[opp] - s1[opp])
        rows.append(dict(date=g["date_key"], event_id=g["event_id"], team=g[side]["name"], opp=g[opp]["name"],
            bucket=None if lx is None or lx == 0 else ("home_" if side == "home" else "road_") + ("fav" if lx > 0 else "dog"),
            neutral=g.get("neutral_site"), gap1=b1[side]["fg_pct"] - b1[opp]["fg_pct"], pre_line=lx, live_line=ll,
            live_moved=None if ll is None or lx is None else abs(ll - lx) > 0.01, ht_margin=ht_m, h2_margin=h2,
            cover_half=None if lx is None else (np.sign(h2 - 0.5 * lx) + 1) / 2 if h2 != 0.5 * lx else 0.5,
            cover_live=None if ll is None else (np.sign(h2 - (ll - ht_m)) + 1) / 2 if h2 != ll - ht_m else 0.5))
df = pd.DataFrame(rows)
if df.empty:
    print("no completed games with halftime snapshots yet"); raise SystemExit
df.to_csv("data/team_rows.csv", index=False)
ap = argparse.ArgumentParser(); ap.add_argument("--gap", type=float, default=10); a = ap.parse_args()
print(f"{df.event_id.nunique()} games, {len(df)} team-rows; live line differs from pregame in "
      f"{df.live_moved.mean():.0%} of rows (if ~0%, ESPN's halftime odds are just the pregame line)")
cold = df[(df.gap1 <= -a.gap) & df.bucket.notna()]
agg = lambda x: pd.Series(dict(n=len(x), avg_ht_margin=x.ht_margin.mean(), avg_2h_margin=x.h2_margin.mean(),
                               cover_half=x.cover_half.mean(), cover_live=x.cover_live.mean(), n_live=x.cover_live.notna().sum()))
print(f"\nTeams that shot {a.gap:g}+ pts worse in 1H:"); print(cold.groupby("bucket").apply(agg).round(3).to_string())
print("\nAll cold:"); print(agg(cold).round(3).to_string())
