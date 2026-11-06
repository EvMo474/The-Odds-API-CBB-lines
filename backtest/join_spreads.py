#!/usr/bin/env python3
"""
Attach pregame point spreads and totals to the ESPN-based game table.

Lines come from the free open dataset github.com/zachmayer/point_spreads
(data/spreads_and_totals.csv: game_date, home_team, away_team, spread, total;
mostly Covers.com, Nov 2010 - Apr 2024). `spread` is from the HOME team's
perspective: negative = home favored (-7.5 means home favored by 7.5).

Teams are matched on ESPN's stable team IDs (not names, which change over time):
  1. exact/alias/fuzzy name match to seed a name -> team-id map
  2. learned from games: same date + same home team in both sources => the away names
     are the same team (and vice versa)
Then each line row is joined to a game on date + both team ids (+-1 day; home/away
swapped rows are fixed and the spread negated).

Usage:
    python join_spreads.py cbb_first_half.csv --out cbb_with_lines.csv
    python join_spreads.py cbb_first_half.csv --spreads my_other_lines.csv   # same columns

Adds: spread_home, spread_away (= -spread_home), total, line_swapped, line_date_offset.
Needs: pandas (+ requests if downloading).
"""
import argparse
import difflib
import re
import sys
from collections import Counter

import numpy as np
import pandas as pd

URL = "https://raw.githubusercontent.com/zachmayer/point_spreads/HEAD/data/spreads_and_totals.csv"

# normalized line-source name -> normalized ESPN name; used only if that ESPN name exists
ALIASES = {
    "brigham young": "byu", "connecticut": "uconn", "massachusetts": "umass", "pitt": "pittsburgh",
    "etsu": "east tennessee state", "miami fl": "miami", "miami oh": "miami ohio",
    "cs fullerton": "cal state fullerton", "cs northridge": "cal state northridge",
    "cs bakersfield": "cal state bakersfield", "loyola il": "loyola chicago",
}


def norm(s):
    s = str(s).lower().replace("&", " and ").replace("'", "").replace(".", " ").replace("-", " ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    t = s.split()
    if t and t[0] == "st":
        t[0] = "saint"
    if len(t) > 1 and t[-1] == "st":
        t[-1] = "state"
    t = ["university" if x == "univ" else x for x in t]
    return " ".join(t)


def load_lines(src):
    if src is None:
        import requests
        from io import StringIO
        print(f"downloading {URL}", file=sys.stderr)
        r = requests.get(URL, timeout=120, headers={"User-Agent": "cbb-lines/1.0"})
        r.raise_for_status()
        sp = pd.read_csv(StringIO(r.text))
    else:
        sp = pd.read_csv(src)
    need = {"game_date", "home_team", "away_team", "spread"}
    if not need <= set(sp.columns):
        sys.exit(f"lines file needs columns {sorted(need)}; has {list(sp.columns)}")
    if "total" not in sp:
        sp["total"] = np.nan
    sp["date"] = pd.to_datetime(sp["game_date"], errors="coerce")
    sp = sp.dropna(subset=["date"])
    sort = [c for c in ("date", "updated_date") if c in sp]
    sp = sp.sort_values(sort).drop_duplicates(["date", "home_team", "away_team"], keep="last")
    sp["h"] = sp.home_team.map(norm)
    sp["a"] = sp.away_team.map(norm)
    return sp


def espn_name_ids(es):
    """normalized ESPN name -> team id (most common id for that name)."""
    c = {}
    for name, tid in list(zip(es.home_name, es.home_id)) + list(zip(es.away_name, es.away_id)):
        c.setdefault(norm(name), Counter())[tid] += 1
    return {k: v.most_common(1)[0][0] for k, v in c.items()}


def seed_map(sp, name2id):
    nm, confirmed = {}, set()
    keys = sorted(name2id)
    for c in sorted(set(sp.h) | set(sp.a)):
        if c in name2id:
            nm[c] = name2id[c]
            confirmed.add(c)
        elif c in ALIASES and norm(ALIASES[c]) in name2id:
            nm[c] = name2id[norm(ALIASES[c])]
            confirmed.add(c)
        else:
            m = difflib.get_close_matches(c, keys, n=1, cutoff=0.72)
            if m:
                nm[c] = name2id[m[0]]
    return nm, confirmed


def learn_names(sp, e, nm, confirmed, rounds=3, min_votes=3, min_share=0.6):
    """e: ESPN games with columns edate, hid, aid. Adopt consistent name -> id pairings."""
    sp = sp.copy()
    ee = e[["edate", "hid", "aid"]].rename(columns={"edate": "date"})
    learned = 0
    for _ in range(rounds):
        sp["hid"] = sp.h.map(nm)
        sp["aid"] = sp.a.map(nm)
        votes = {}
        x = sp.merge(ee, on=["date", "hid"], how="inner", suffixes=("", "_e"))   # same home -> away names
        for c, v in zip(x.a, x.aid_e):
            if c not in confirmed:
                votes.setdefault(c, Counter())[v] += 1
        y = sp.merge(ee, on=["date", "aid"], how="inner", suffixes=("", "_e"))   # same away -> home names
        for c, v in zip(y.h, y.hid_e):
            if c not in confirmed:
                votes.setdefault(c, Counter())[v] += 1
        new = 0
        for c, cnt in votes.items():
            best, n = cnt.most_common(1)[0]
            if n >= min_votes and n / sum(cnt.values()) >= min_share:
                if nm.get(c) != best:
                    nm[c] = best
                    new += 1
                confirmed.add(c)
        learned += new
        if new == 0:
            break
    return nm, learned


def join(games, sp):
    e = games[["game_id", "date", "home_id", "away_id", "home_name", "away_name"]].copy()
    e["edate"] = pd.to_datetime(e["date"], errors="coerce")
    e = e.rename(columns={"home_id": "hid", "away_id": "aid"})
    name2id = espn_name_ids(games)
    nm, confirmed = seed_map(sp, name2id)
    nm, learned = learn_names(sp, e, nm, confirmed)
    print(f"team mapping: {len(confirmed)} confirmed, {learned} learned from shared games, "
          f"{len(set(sp.h) | set(sp.a)) - len(nm)} unmapped", file=sys.stderr)

    s = sp.copy()
    s["hid"] = s.h.map(nm)
    s["aid"] = s.a.map(nm)
    s = s.dropna(subset=["hid", "aid"])
    s[["hid", "aid"]] = s[["hid", "aid"]].astype("int64")
    base = e[["game_id", "edate", "hid", "aid"]].copy()
    base[["hid", "aid"]] = base[["hid", "aid"]].astype("int64")

    found = []
    for swapped in (False, True):
        for off in (0, 1, -1):
            x = s.copy()
            x["edate"] = x.date + pd.Timedelta(days=off)
            if swapped:
                x = x.rename(columns={"hid": "aid", "aid": "hid"})
            r = x.merge(base, on=["edate", "hid", "aid"], how="inner")
            r["line_swapped"] = swapped
            r["line_date_offset"] = off
            found.append(r)
    r = pd.concat(found, ignore_index=True)
    r["_k"] = r.line_swapped.astype(int) * 10 + r.line_date_offset.abs()
    r = r.sort_values("_k").drop_duplicates("game_id")
    r["spread_home"] = np.where(r.line_swapped, -r.spread, r.spread)
    out = games.merge(r[["game_id", "spread_home", "total", "line_swapped", "line_date_offset"]],
                      on="game_id", how="left")
    out["spread_away"] = -out["spread_home"]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("games_csv")
    ap.add_argument("--spreads", help="local lines CSV (default: download the zachmayer/point_spreads file)")
    ap.add_argument("--out", default="cbb_with_lines.csv")
    a = ap.parse_args()

    games = pd.read_csv(a.games_csv, low_memory=False)
    for c in ("game_id", "date", "home_id", "away_id", "home_name", "away_name"):
        if c not in games:
            sys.exit(f"{a.games_csv} needs column {c}")
    out = join(games, load_lines(a.spreads))

    has = out.spread_home.notna()
    print(f"games: {len(out)}   with a line: {int(has.sum())} ({has.mean():.1%})   "
          f"home/away swapped fixes: {int(out.line_swapped.fillna(False).sum())}")
    if "season" in out:
        t = out.groupby("season").agg(games=("game_id", "size"),
                                      with_line=("spread_home", lambda s: s.notna().sum()))
        t["pct"] = (t.with_line / t.games * 100).round(1)
        print(t.to_string())
    if {"ht_home", "ht_away"} <= set(out.columns):
        v = out.dropna(subset=["spread_home"])
        b = np.polyfit(v.spread_home, v.ht_home - v.ht_away, 1)
        print(f"sanity: halftime home margin vs spread slope {b[0]:.3f} (want about -0.5), n={len(v)}")
    miss = out[~has & (out.get("season", 0) <= 2024)]
    top = pd.concat([miss.home_name, miss.away_name]).value_counts().head(12)
    print("\nESPN teams most often without a line, seasons <= 2024 (the line source simply lacks many "
          "games for smaller programs; a name mismatch would show a team missing nearly ALL games):")
    print(top.to_string())
    out.to_csv(a.out, index=False)
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
