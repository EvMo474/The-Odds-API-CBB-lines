#!/usr/bin/env python3
"""
Flatten data/games/*/*.json into data/team_rows.csv (one row per game, from the COLD-shooting team's view)
and print the same backtest we ran on history, live.

  python report.py                 # cold = team with lower 1H FG%, gap >= 10 points
  python report.py --gap 12 --shift 0.5

Line used: the DraftKings / FanDuel halftime spread the logger saved closest to 'first seen at halftime + 3.5 min'
(about 5 minutes after the first half ended, matching the historical backtest).
Cover = cold team's 2nd-half margin beats (expected final margin from the book - halftime margin).
"""
import argparse, glob, json
from datetime import datetime
import numpy as np, pandas as pd

ap = argparse.ArgumentParser(); ap.add_argument("--gap", type=float, default=10); ap.add_argument("--shift", type=float, default=0.0)
ap.add_argument("--minutes", type=float, default=3.5, help="minutes after the logger first saw halftime to take the line")
a = ap.parse_args()
T = lambda s: datetime.fromisoformat(s)

rows = []
for f in sorted(glob.glob("data/games/*/*.json")):
    g = json.load(open(f))
    ht, fin = g.get("halftime"), g.get("final")
    if not ht or not fin or not fin.get("box_full") or fin.get("periods", 2) > 2:
        continue
    b1 = ht["box_1h"]
    if not (b1.get("home", {}).get("fga") and b1.get("away", {}).get("fga")):
        continue
    gap = b1["away"]["fg_pct"] - b1["home"]["fg_pct"]          # away - home; > 0 => home is the cold team
    s = 1 if gap > 0 else -1
    s1 = {k: int(ht["score"][k]) for k in ("home", "away")}; sf = {k: int(fin["score"][k]) for k in ("home", "away")}
    ht_cold = s * (s1["home"] - s1["away"]); h2_cold = s * ((sf["home"] - s1["home"]) - (sf["away"] - s1["away"]))
    ticks = [t for t in g.get("odds_api_ticks", []) if not t.get("unmatched")]
    row = dict(date=g["date_key"], event_id=g["event_id"], home=g["home"]["name"], away=g["away"]["name"],
               cold_side="home" if s == 1 else "road", gap=abs(gap), ht_cold=ht_cold, h2_cold=h2_cold, n_ticks=len(ticks))
    if ticks and g.get("halftime_first_seen"):
        target = T(g["halftime_first_seen"]).timestamp() + a.minutes * 60
        t = min(ticks, key=lambda x: abs(T(x["t"]).timestamp() - target))
        row["tick_min_after_first_seen"] = round((T(t["t"]).timestamp() - T(g["halftime_first_seen"]).timestamp()) / 60, 1)
        for bk, short in (("draftkings", "dk"), ("fanduel", "fd")):
            v = t.get(f"{bk}_home_spread")
            if v is not None:
                row[f"{short}_imp"] = s * (-v) - ht_cold          # implied 2H expected margin, cold team
    rows.append(row)
df = pd.DataFrame(rows)
if df.empty:
    print("no completed games with halftime snapshots yet"); raise SystemExit
for c in ("dk_imp", "fd_imp"):
    if c not in df: df[c] = np.nan
df["best_imp"] = df[["dk_imp", "fd_imp"]].min(axis=1)
exp_final = (df.dk_imp.fillna(df.fd_imp) + df.ht_cold)
df["bucket"] = df.cold_side + "_" + np.where(exp_final > 0, "fav", "dog")
df.to_csv("data/team_rows.csv", index=False)
print(f"{len(df)} finished games logged; with a DK/FD halftime line: {int(df[['dk_imp','fd_imp']].notna().any(axis=1).sum())}")

def stats(x, col):
    x = x.dropna(subset=[col]); m = x.h2_cold - (x[col] + a.shift)
    w, l = int((m > 0).sum()), int((m < 0).sum()); n = w + l
    return dict(n=n, W=w, L=l, cover=w / n if n else np.nan, roi=(w * 0.9091 - l) / n if n else np.nan, avg_margin=m.mean())
cold = df[df.gap >= a.gap]
print(f"\nCold team = 1H FG% gap >= {a.gap:g} pts, line shift {a.shift:+g}; break-even 52.4%")
out = []
for k in ["road_dog", "home_fav", "road_fav", "home_dog", "ALL"]:
    x = cold if k == "ALL" else cold[cold.bucket == k]
    for c in ("dk_imp", "fd_imp", "best_imp"):
        r = stats(x, c); r.update(bucket=k, line=c.split("_")[0]); out.append(r)
print(pd.DataFrame(out).set_index(["bucket", "line"]).round(3).to_string())
