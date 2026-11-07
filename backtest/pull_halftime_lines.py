#!/usr/bin/env python3
"""
(v2: --seasons list, shared budget, --include-neutral, --fill-pregame for seasons/games with no closing line, checkpoints.)
Pull live halftime lines (all six of your books) from The Odds API history for a chosen set of games.

  python backtest/pull_halftime_lines.py --mode plan                      # counts + credit estimate, spends nothing
  python backtest/pull_halftime_lines.py --mode busy   --n 60             # the 60 busiest halftime moments (~600 credits)
  python backtest/pull_halftime_lines.py --mode random --n 300            # random games (~2,800 credits)
  python backtest/pull_halftime_lines.py --mode all --budget 5000        # every qualifying game, oldest first, spend at most 5,000 credits this run
                                                                          # (rerun to continue: moments already pulled are cached and free)

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


def find_event(events, hname, aname, tip, allow_swap=False):
    """returns (event, score, swapped).  swapped = the book lists the two teams the other way round
    (only tried for neutral-site games, where ESPN and the books can disagree about who is "home")."""
    hn, an = norm(hname), norm(aname)
    best, score, swapped = None, 0, False
    for e in events:
        ct = pd.Timestamp(e["commence_time"])
        if abs((ct - tip).total_seconds()) > 8 * 3600:
            continue
        eh, ea = norm(e["home_team"]), norm(e["away_team"])
        s = (difflib.SequenceMatcher(None, hn, eh).ratio() + difflib.SequenceMatcher(None, an, ea).ratio()) / 2
        if s > score:
            best, score, swapped = e, s, False
        if allow_swap:
            s2 = (difflib.SequenceMatcher(None, hn, ea).ratio() + difflib.SequenceMatcher(None, an, eh).ratio()) / 2
            if s2 > score:
                best, score, swapped = e, s2, True
    return (best, score, swapped) if score >= 0.82 else (None, score, False)


def candidates(out, season, gap, include_neutral, fill_pregame):
    g = pd.read_csv(out / "cbb_backtest_games.csv.gz", low_memory=False)
    t = pd.read_csv(out / "game_times.csv.gz", parse_dates=["tip_sched_utc", "ht_start_utc", "start2h_utc", "snap_utc"])
    neutral = g.neutral_site.astype(str).str.lower().isin(["true", "1", "1.0"])
    has = g.spread_home.notna() & ~g.bucket.isin(["Pick", ""])
    m = (g.season == season) & (g.gap >= gap) & (include_neutral | ~neutral)
    m &= (has | g.spread_home.isna()) if fill_pregame else has
    m &= g.pbp_complete_1h.astype(str).str.lower().eq("true") & g.pts_match_1h.astype(str).str.lower().eq("true")
    g = g[m].assign(neutral=neutral[m], needs_pre=~has[m]).merge(
        t[["game_id", "tip_sched_utc", "ht_start_utc", "start2h_utc", "snap_utc", "ht_src"]], on="game_id", how="inner")
    g = g.dropna(subset=["snap_utc"]).copy()
    g["slot"] = g.snap_utc.dt.round("5min") + pd.Timedelta(minutes=1)          # +1 min: the API returns the snapshot on that 5-min mark
    g["pre_slot"] = (g.tip_sched_utc - pd.Timedelta(minutes=10)).dt.floor("5min") + pd.Timedelta(minutes=1)
    names = display_names(season, out.parent.parent / "backtest_work" / "standings_cache")
    g["home_disp"] = g.home_id.map(names).fillna(g.home_name); g["away_disp"] = g.away_id.map(names).fillna(g.away_name)
    return g


def fname(cache, s):
    return cache / (s.strftime("%Y%m%dT%H%M%SZ") + ".json.gz")


def checkpoint(msg):
    """commit + push what has been saved so far (only on the Actions runner), so a timeout never loses paid-for snapshots"""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    import subprocess
    sh = lambda c: subprocess.run(c, shell=True, capture_output=True, text=True)
    sh('git config user.name cbb-backtest; git config user.email cbb-backtest@users.noreply.github.com')
    sh("git add backtest/out"); r = sh(f'git commit -m "{msg}"')
    if "nothing to commit" in (r.stdout + r.stderr):
        return
    for _ in range(5):
        if sh("git pull --rebase origin main && git push origin HEAD:main").returncode == 0:
            break
        time.sleep(8)


def fetch_many(slots, cache, key, state, a):
    """pull uncached snapshot moments (6 at a time); stops on budget, low credits or an unexpected price"""
    from concurrent.futures import ThreadPoolExecutor
    todo = [s for s in slots if not fname(cache, s).exists()]
    if not todo:
        return
    def one(sl):
        data, hd = api(f"/historical/sports/{SPORT}/odds", key, date=sl.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       bookmakers=",".join(BOOKS), markets="spreads", oddsFormat="american")
        fname(cache, sl).write_bytes(gzip.compress(json.dumps(data).encode()))
        return hd
    i = 0
    while i < len(todo):
        if state["spent"] + 10 > a.budget:
            print(f"budget of {a.budget:,} credits reached for this run"); state["stop"] = True; return
        n = 1 if state["first"] else min(6, (a.budget - state["spent"]) // 10, len(todo) - i)
        batch = todo[i:i + n]; i += n
        with ThreadPoolExecutor(n) as ex:
            hds = list(ex.map(one, batch))
        for hd in hds:
            state["spent"] += 10; state["n"] += 1
            rem, last = hd.get("x-requests-remaining"), hd.get("x-requests-last")
            if rem is not None:
                state["remaining"] = int(float(rem))
            if state["first"]:
                print(f"first call cost: x-requests-last={last}, credits remaining {rem}", flush=True); state["first"] = False
                if last is not None and float(last) > 10:
                    sys.exit(f"STOP: a single call cost {last} credits, more than the 10 expected. Nothing more was spent.")
        if state["n"] % 60 < n:
            print(f"  {state['n']} moments pulled this run, credits remaining {state['remaining']}", flush=True)
        if state["n"] % 600 < n:
            checkpoint(f"odds cache checkpoint {state['n']}")
        if state["remaining"] is not None and state["remaining"] < a.min_credits:
            print(f"stopping: only {state['remaining']} credits left (reserve {a.min_credits})"); state["stop"] = True; return


def load(cache, s):
    f = fname(cache, s)
    return json.loads(gzip.decompress(f.read_bytes())) if f.exists() else None


def build(g, cache, season, out):
    rows, miss, nopre = [], 0, 0
    for r in g.itertuples():
        snap = load(cache, r.slot)
        if not snap:
            continue
        ev, score, swapped = find_event(snap.get("data", []), r.home_disp, r.away_disp, pd.Timestamp(r.tip_sched_utc), allow_swap=bool(r.neutral))
        if ev is None:
            miss += 1; continue
        ts = pd.Timestamp(snap["timestamp"]); ca = r.cold_side == "away"
        hs = {b: book_spread(ev, b) for b in BOOKS}
        if swapped:
            hs = {b: (None if v is None else -v) for b, v in hs.items()}
        sh, src, pre = r.spread_home, "table", {}
        if r.needs_pre:                                            # closing line from an Odds API snapshot ~10 min before tip
            ps = load(cache, r.pre_slot)
            pev = find_event(ps.get("data", []), r.home_disp, r.away_disp, pd.Timestamp(r.tip_sched_utc), allow_swap=bool(r.neutral)) if ps else (None, 0, False)
            if pev[0] is None:
                nopre += 1; continue
            pre = {b: book_spread(pev[0], b) for b in BOOKS}
            if pev[2]:
                pre = {b: (None if v is None else -v) for b, v in pre.items()}
            pv = [v for v in pre.values() if v is not None]
            if not pv:
                nopre += 1; continue
            sh, src = float(np.median(pv)), "oddsapi_pre"
        cs0 = -sh if ca else sh                                    # + = cold team is the dog
        bucket = "Pick" if cs0 == 0 else (("Road Dog" if ca else "Home Dog") if cs0 > 0 else ("Road Fav" if ca else "Home Fav"))
        d = dict(game_id=r.game_id, season=season, date=r.game_date_et, home=r.home_name, away=r.away_name, cold_team=r.cold_team,
                 bucket=bucket, gap=r.gap, close_spread_home=sh, close_src=src, cold_spread_pre=cs0, cold_margin_1h=r.cold_margin_1h,
                 cold_margin_2h=r.cold_margin_2h, ht_src=r.ht_src, snapshot=ts,
                 min_into_break=round((ts - r.ht_start_utc).total_seconds() / 60, 1),
                 in_break=bool(r.ht_start_utc <= ts <= (r.start2h_utc if pd.notna(r.start2h_utc) else r.ht_start_utc + pd.Timedelta(minutes=17))),
                 name_match=round(score, 2), neutral=bool(r.neutral), swapped=swapped, odds_event_id=ev["id"])
        for b, v in pre.items():
            d[f"pre_{LABEL[b]}"] = v
        for b, v in hs.items():
            d[f"home_{LABEL[b]}"] = v
            if v is not None:
                l2 = (-v if ca else v) + r.cold_margin_1h          # the 2nd-half line for the cold team
                d[f"l2_{LABEL[b]}"] = l2
                x = r.cold_margin_2h + l2
                d[f"res_{LABEL[b]}"] = "W" if x > 0 else "L" if x < 0 else "P"
        got = [v for v in hs.values() if v is not None]
        d["n_books"] = len(got)
        if got:
            med = float(np.median(got))
            exp_home = sh * 0.5 + r.cold_margin_1h * (1 if ca else -1)
            d["dev_vs_expected"] = round(med - exp_home, 1)
            d["orientation_ok"] = abs(med - exp_home) <= 12
            d["mkt_shift"] = round(float(np.median([d[f"l2_{LABEL[b]}"] for b in BOOKS if f"l2_{LABEL[b]}" in d])) - cs0 / 2, 1)
        rows.append(d)
    res = pd.DataFrame(rows)
    res.to_csv(out / f"halftime_lines_{season}.csv", index=False)
    L = [f"season {season}: {len(g)} candidate games; rows built {len(res)}; no Odds API match {miss}; no closing line found {nopre}"]
    if len(res):
        L.append(f"  any live line {(res.n_books > 0).mean():.0%}; median books {res[res.n_books > 0].n_books.median():.0f}; "
                 f"in break {res.in_break.mean():.0%}; closing line filled from Odds API: {(res.close_src != 'table').sum()}")
        ok = res[res["orientation_ok"] == True] if "orientation_ok" in res else res.iloc[0:0]
        L.append(f"  passes orientation check {len(ok)}; mean market shift {ok.mkt_shift.mean():+.2f}" if len(ok) else "  no orientation-ok rows")
        for b in ("DK", "FD"):
            if len(ok) and f"res_{b}" in ok:
                w, l = (ok[f"res_{b}"] == "W").sum(), (ok[f"res_{b}"] == "L").sum()
                L.append(f"  {b}: {w}-{l} ({w / max(w + l, 1):.1%})")
    return L


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["plan", "all"], default="plan")
    ap.add_argument("--seasons", default="2026", help="comma list, processed in order, e.g. 2026,2024,2023,2022,2025")
    ap.add_argument("--gap", type=float, default=10)
    ap.add_argument("--out", default="backtest/out")
    ap.add_argument("--min-credits", type=int, default=3000, help="stop if remaining credits fall below this")
    ap.add_argument("--budget", type=int, default=5000, help="most credits to spend in this run")
    ap.add_argument("--include-neutral", action="store_true")
    ap.add_argument("--fill-pregame", default="", help="comma list of seasons where games with no closing line are also taken (closing line comes from an Odds API snapshot ~10 min before tip), or 'all'")
    a = ap.parse_args()
    out = Path(a.out); cache = out / "odds_cache"; cache.mkdir(parents=True, exist_ok=True)
    seasons = [int(x) for x in a.seasons.split(",") if x.strip()]
    fill = {int(x) for x in a.fill_pregame.split(",") if x.strip().isdigit()}
    G = {s: candidates(out, s, a.gap, a.include_neutral, (a.fill_pregame == "all") or s in fill) for s in seasons}
    tot = 0
    for s, g in G.items():
        ht = set(g.slot); pre = set(g[g.needs_pre].pre_slot)
        nh, npre = sum(not fname(cache, x).exists() for x in ht), sum(not fname(cache, x).exists() for x in pre)
        tot += (nh + npre) * 10
        print(f"season {s}: {len(g)} games, {len(ht)} halftime moments ({nh} uncached), {len(pre)} pregame moments ({npre} uncached) -> {(nh + npre) * 10:,} credits")
    print(f"TOTAL still to buy: {tot:,} credits (this run's budget {a.budget:,})")
    if a.mode == "plan":
        return
    key = os.environ.get("ODDS_API_KEY") or sys.exit("set ODDS_API_KEY")
    state = dict(spent=0, n=0, remaining=None, first=True, stop=False)
    for s, g in G.items():
        if state["stop"]:
            break
        print(f"--- season {s}", flush=True)
        fetch_many(sorted(set(g[g.needs_pre].pre_slot)), cache, key, state, a)
        if not state["stop"]:
            fetch_many(sorted(set(g.slot)), cache, key, state, a)
    L = [f"run spent {state['spent']:,} credits on {state['n']} moments; credits remaining {state['remaining']}"]
    for s, g in G.items():
        L += build(g, cache, s, out)
    (out / "halftime_summary_v2.txt").write_text("\n".join(L)); print("\n".join(L))
    checkpoint("halftime lines results")


if __name__ == "__main__":
    main()
