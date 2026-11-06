#!/usr/bin/env python3
"""
Pull live halftime lines (all six of your books) from The Odds API history for a chosen set of games.

  python backtest/pull_halftime_lines.py --mode plan                      # counts + credit estimate, spends nothing
  python backtest/pull_halftime_lines.py --mode busy   --n 60             # the 60 busiest halftime moments (~600 credits)
  python backtest/pull_halftime_lines.py --mode random --n 300            # random games (~2,800 credits)

One call (10 credits) returns EVERY game's lines at that moment, so games that reach halftime together share it.
Safety: refuses to start if the estimate exceeds --max-credits; checks the cost of the first call (x-requests-last)
and stops if it is more than 10; stops if remaining credits fall below --min-credits.
Raw snapshots are saved (gzip) in backtest/out/odds_cache so nothing is ever paid for twice.
Inputs (made by the earlier jobs): backtest/out/cbb_backtest_games.csv.gz and game_times.csv.gz
Output: backtest/out/halftime_lines_<mode>.csv and a printed summary (also saved as halftime_summary_<mode>.txt)
"""
import argparse, difflib, gzip, json, os, re, sys, time
from pathlib import Path
import numpy as np
import pandas as pd

BASE = os.environ.get("ODDS_API_BASE", "https://api.the-odds-api.com/v4")
SPORT = "basketball_ncaab"
BOOKS = ["draftkings", "fanduel", "betmgm", "betrivers", "williamhill_us", "fanatics"]
LABEL = {"draftkings": "DK", "fanduel": "FD", "betmgm": "MGM", "betrivers": "BR", "williamhill_us": "CZR", "fanatics": "FAN"}
SB_URL = "https://github.com/sportsdataverse/hoopR-mbb-data/raw/main/mbb/standings/parquet/standings_{s}.parquet"


def norm(s):
    s = str(s).lower().replace("&", " and ").replace("'", "").replace(".", " ").replace("-", " ")
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())


def display_names(season, cache):
    try:
        import requests
        p = cache / f"standings_{season}.parquet"
        if not p.exists():
            cache.mkdir(parents=True, exist_ok=True)
            r = requests.get(SB_URL.format(s=season), timeout=120); r.raise_for_status(); p.write_bytes(r.content)
        d = pd.read_parquet(p, columns=["team_id", "team_display_name"]).drop_duplicates("team_id")
        return dict(zip(d.team_id, d.team_display_name))
    except Exception as e:
        print(f"  no display names for {season} ({e}); falling back to table names", file=sys.stderr)
        return {}


def api(path, key, **params):
    import requests
    for i in range(4):
        r = requests.get(f"{BASE}{path}", params=dict(apiKey=key, **params), timeout=90)
        if r.status_code == 200:
            return r.json(), r.headers
        if r.status_code in (429, 500, 502, 503):
            time.sleep(3 * (i + 1)); continue
        sys.exit(f"API error {r.status_code}: {r.text[:300]}")
    sys.exit("API kept failing")


def book_spread(ev, book):
    for b in ev.get("bookmakers", []):
        if b["key"] == book:
            for m in b["markets"]:
                if m["key"] == "spreads":
                    for o in m["outcomes"]:
                        if o["name"] == ev["home_team"]:
                            return o["point"]
    return None


def find_event(events, hname, aname, tip):
    hn, an = norm(hname), norm(aname)
    best, score = None, 0
    for e in events:
        ct = pd.Timestamp(e["commence_time"])
        if abs((ct - tip).total_seconds()) > 8 * 3600:
            continue
        s = (difflib.SequenceMatcher(None, hn, norm(e["home_team"])).ratio() +
             difflib.SequenceMatcher(None, an, norm(e["away_team"])).ratio()) / 2
        if s > score:
            best, score = e, s
    return (best, score) if score >= 0.82 else (None, score)


def candidates(out, season, gap):
    g = pd.read_csv(out / "cbb_backtest_games.csv.gz", low_memory=False)
    t = pd.read_csv(out / "game_times.csv.gz", parse_dates=["tip_sched_utc", "ht_start_utc", "start2h_utc", "snap_utc"])
    neutral = g.neutral_site.astype(str).str.lower().isin(["true", "1", "1.0"])
    m = (g.season == season) & (g.gap >= gap) & g.spread_home.notna() & ~g.bucket.isin(["Pick", ""]) & ~neutral
    m &= g.pbp_complete_1h.astype(str).str.lower().eq("true") & g.pts_match_1h.astype(str).str.lower().eq("true")
    g = g[m].merge(t[["game_id", "tip_sched_utc", "ht_start_utc", "start2h_utc", "snap_utc", "ht_src"]], on="game_id", how="inner")
    g = g.dropna(subset=["snap_utc"]).copy()
    g["slot"] = g.snap_utc.dt.round("5min") + pd.Timedelta(minutes=1)   # +1 min: the API returns the snapshot on that 5-min mark
    names = display_names(season, out.parent.parent / "backtest_work" / "standings_cache")
    g["home_disp"] = g.home_id.map(names).fillna(g.home_name); g["away_disp"] = g.away_id.map(names).fillna(g.away_name)
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["plan", "busy", "random"], default="plan")
    ap.add_argument("--n", type=int, default=60, help="busy: number of snapshot moments; random: number of games")
    ap.add_argument("--season", type=int, default=2026); ap.add_argument("--gap", type=float, default=10)
    ap.add_argument("--out", default="backtest/out")
    ap.add_argument("--max-credits", type=int, default=700); ap.add_argument("--min-credits", type=int, default=300)
    a = ap.parse_args()
    out = Path(a.out); cache = out / "odds_cache"; cache.mkdir(parents=True, exist_ok=True)
    g = candidates(out, a.season, a.gap)
    print(f"candidate games (season {a.season}, gap>={a.gap}, non-neutral, clean, with pregame line): {len(g)}; "
          f"moments: {g.slot.nunique()}")
    if a.mode == "busy":
        top = g.groupby("slot").size().sort_values(ascending=False).head(a.n).index
        g = g[g.slot.isin(top)]
    elif a.mode == "random":
        g = g.sample(min(a.n, len(g)), random_state=7)
    slots = sorted(g.slot.unique())
    new = [s for s in slots if not (cache / (s.strftime("%Y%m%dT%H%M%SZ") + ".json.gz")).exists()]
    est = len(new) * 10
    print(f"selected games: {len(g)}; snapshot moments: {len(slots)} ({len(new)} not cached) -> about {est:,} credits")
    if a.mode == "plan":
        return
    if est > a.max_credits:
        sys.exit(f"estimate {est} is above --max-credits {a.max_credits}; refusing to start")
    key = os.environ.get("ODDS_API_KEY") or sys.exit("set ODDS_API_KEY")
    snaps, remaining = {}, None
    for i, sl in enumerate(slots, 1):
        iso = sl.strftime("%Y-%m-%dT%H:%M:%SZ"); f = cache / (sl.strftime("%Y%m%dT%H%M%SZ") + ".json.gz")
        if f.exists():
            snaps[sl] = json.loads(gzip.decompress(f.read_bytes())); continue
        data, hd = api(f"/historical/sports/{SPORT}/odds", key, date=iso, bookmakers=",".join(BOOKS),
                       markets="spreads", oddsFormat="american")
        f.write_bytes(gzip.compress(json.dumps(data).encode())); snaps[sl] = data
        remaining, last = hd.get("x-requests-remaining"), hd.get("x-requests-last")
        if i == 1:
            print(f"first call cost: x-requests-last={last}, credits remaining {remaining}", flush=True)
            if last is not None and float(last) > 10:
                sys.exit(f"STOP: a single call cost {last} credits, more than the 10 expected. Nothing more was spent.")
        if i % 10 == 0:
            print(f"  {i}/{len(slots)} moments, credits remaining {remaining}", flush=True)
        if remaining is not None and int(float(remaining)) < a.min_credits:
            print(f"stopping: only {remaining} credits left"); break

    rows, miss = [], 0
    for r in g.itertuples():
        snap = snaps.get(r.slot)
        if not snap:
            continue
        ev, score = find_event(snap.get("data", []), r.home_disp, r.away_disp, pd.Timestamp(r.tip_sched_utc))
        if ev is None:
            miss += 1; continue
        ts = pd.Timestamp(snap["timestamp"])
        ca = r.cold_side == "away"
        d = dict(game_id=r.game_id, date=r.game_date_et, home=r.home_name, away=r.away_name, cold_team=r.cold_team, bucket=r.bucket,
                 gap=r.gap, cold_spread_pre=r.cold_spread, cold_margin_1h=r.cold_margin_1h, cold_margin_2h=r.cold_margin_2h,
                 ht_src=r.ht_src, snapshot=ts, min_into_break=round((ts - r.ht_start_utc).total_seconds() / 60, 1),
                 in_break=bool(r.ht_start_utc <= ts <= (r.start2h_utc if pd.notna(r.start2h_utc) else r.ht_start_utc + pd.Timedelta(minutes=17))),
                 name_match=round(score, 2), odds_event_id=ev["id"])
        hs = {b: book_spread(ev, b) for b in BOOKS}
        for b, v in hs.items():
            d[f"home_{LABEL[b]}"] = v
            if v is not None:
                cs = -v if ca else v                       # live full-game spread from the cold team's side
                l2 = cs + r.cold_margin_1h                 # = the 2nd-half line for the cold team
                d[f"l2_{LABEL[b]}"] = l2
                x = r.cold_margin_2h + l2
                d[f"res_{LABEL[b]}"] = "W" if x > 0 else "L" if x < 0 else "P"
        got = [v for v in hs.values() if v is not None]
        d["n_books"] = len(got)
        if got:
            med = float(np.median(got))
            exp_home = r.spread_home * 0.5 + r.cold_margin_1h * (1 if ca else -1)   # home spread if the market just halves the close and adds the 1H score
            d["dev_vs_expected"] = round(med - exp_home, 1)
            d["orientation_ok"] = abs(med - exp_home) <= 12
            d["mkt_shift"] = round(float(np.median([d[f"l2_{LABEL[b]}"] for b in BOOKS if f"l2_{LABEL[b]}" in d])) - r.cold_spread / 2, 1)
        rows.append(d)
    res = pd.DataFrame(rows); fn = out / f"halftime_lines_{a.mode}.csv"; res.to_csv(fn, index=False)

    L = [f"mode {a.mode}: {len(g)} games selected, {len(slots)} moments, credits remaining {remaining}",
         f"matched to an Odds API event: {len(res)} (no match: {miss})"]
    if len(res):
        L.append(f"at least one book had a live line: {int((res.n_books > 0).sum())} ({(res.n_books > 0).mean():.0%})")
        L.append("share with a line, by book: " + ", ".join(f"{LABEL[b]} {res[f'home_{LABEL[b]}'].notna().mean():.0%}" for b in BOOKS))
        L.append(f"books per game (median, games with any): {res[res.n_books > 0].n_books.median():.0f}")
        L.append(f"snapshot inside the break: {res.in_break.mean():.0%}; minutes into break median {res.min_into_break.median():.1f} "
                 f"(5th-95th pct {res.min_into_break.quantile(.05):.1f} to {res.min_into_break.quantile(.95):.1f})")
        ok = res[res.get("orientation_ok", True) == True]
        L.append(f"passes the live-line sanity check: {len(ok)} of {int((res.n_books > 0).sum())}")
        if len(ok):
            L.append(f"market shift (median 2H line minus half the closing spread): mean {ok.mkt_shift.mean():+.2f}, median {ok.mkt_shift.median():+.2f}  [n={len(ok)}]")
            for b in ("DK", "FD"):
                c = f"res_{b}"
                if c in ok:
                    w, l = (ok[c] == "W").sum(), (ok[c] == "L").sum()
                    L.append(f"{b}: {w}-{l}" + (f" ({w / (w + l):.1%} cover)" if w + l else ""))
    (out / f"halftime_summary_{a.mode}.txt").write_text("\n".join(L)); print("\n".join(L))


if __name__ == "__main__":
    main()
