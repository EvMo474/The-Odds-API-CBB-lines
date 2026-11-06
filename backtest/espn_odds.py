#!/usr/bin/env python3
"""
Fill in missing pregame spreads from ESPN (free).  hoopR's game_id IS the ESPN event id, so no name matching.

  1. scoreboard per date (one call covers every game that day; carries the odds block when ESPN kept it)
  2. for games still missing, the per-game summary (pickcenter / odds)  [--summary-limit caps these calls]

Adds / fills:  espn_spread_home (neg = home favored), espn_total, espn_provider, espn_src ("scoreboard"|"summary"|"")
Results are cached in --cache (json) so a re-run resumes instead of starting over.
"""
import argparse, json, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import pandas as pd
import requests

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball"
S = requests.Session(); S.headers["User-Agent"] = "cbb-backtest/1.0"


def parse_odds(odds_list, home_abbr, away_abbr):
    """[{provider, home_spread (neg = home favored), total}] from ESPN odds / pickcenter items."""
    out = []
    for o in odds_list or []:
        det = o.get("details") or ""
        hs = None
        m = re.match(r"\s*([A-Za-z0-9&.\-' ]+?)\s+([+-]?\d+(?:\.\d+)?)\s*$", det)
        if m:
            fav, num = m.group(1).strip().upper(), float(m.group(2))
            if fav == (home_abbr or "").upper(): hs = num
            elif fav == (away_abbr or "").upper(): hs = -num
        if hs is None and "EVEN" in det.upper(): hs = 0.0
        if hs is None and o.get("spread") is not None:
            sp = abs(float(o["spread"]))
            if (o.get("homeTeamOdds") or {}).get("favorite"): hs = -sp
            elif (o.get("awayTeamOdds") or {}).get("favorite"): hs = sp
        if hs is not None:
            out.append({"provider": (o.get("provider") or {}).get("name"), "home_spread": hs,
                        "total": o.get("overUnder")})
    return out


def best(lines):
    """prefer DraftKings, then anything"""
    if not lines: return None
    for l in lines:
        if "draftkings" in str(l["provider"]).lower(): return l
    return lines[0]


def get(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=30)
            if r.status_code == 200: return r.json()
            if r.status_code in (404, 400): return None
        except requests.RequestException:
            pass
        time.sleep(1.5 * (i + 1))
    return None


def abbrs(comps):
    h = a = None
    for c in comps or []:
        ab = (c.get("team") or {}).get("abbreviation")
        if c.get("homeAway") == "home": h = ab
        elif c.get("homeAway") == "away": a = ab
    return h, a


def scoreboard_day(day):                       # day = pandas Timestamp
    j = get(f"{BASE}/scoreboard", {"dates": day.strftime("%Y%m%d"), "groups": 50, "limit": 400})
    res = {}
    for ev in (j or {}).get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        h, a = abbrs(comp.get("competitors"))
        b = best(parse_odds(comp.get("odds"), h, a))
        if b: res[str(ev["id"])] = b
    return res


def summary_game(gid):
    j = get(f"{BASE}/summary", {"event": gid})
    if not j: return gid, None
    comps = ((j.get("header") or {}).get("competitions") or [{}])[0].get("competitors")
    h, a = abbrs(comps)
    items = (j.get("pickcenter") or []) + (j.get("odds") or [])
    return gid, best(parse_odds(items, h, a))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("games_csv"); ap.add_argument("--out", required=True)
    ap.add_argument("--cache", default="espn_odds_cache.json")
    ap.add_argument("--summary-limit", type=int, default=20000)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    g = pd.read_csv(a.games_csv, low_memory=False)
    g["game_id"] = g.game_id.astype(str)
    cp = Path(a.cache)
    cache = json.loads(cp.read_text()) if cp.exists() else {"sb": {}, "sum": {}, "days": []}
    need = g[g.spread_home.isna()] if "spread_home" in g else g
    print(f"{len(need)} of {len(g)} games have no pregame line yet", file=sys.stderr)

    dts = pd.to_datetime(need.date, utc=True, errors="coerce").dt.tz_convert("America/New_York").dt.normalize().dt.tz_localize(None)
    days = sorted(set(dts.dropna()) | {d - timedelta(days=1) for d in dts.dropna()})
    days = [d for d in days if d.strftime("%Y%m%d") not in cache["days"]]
    print(f"scoreboard: {len(days)} dates to fetch", file=sys.stderr)
    with ThreadPoolExecutor(a.workers) as ex:
        for d, r in zip(days, ex.map(scoreboard_day, days)):
            cache["sb"].update(r); cache["days"].append(d.strftime("%Y%m%d"))
            if len(cache["days"]) % 50 == 0: cp.write_text(json.dumps(cache))
    cp.write_text(json.dumps(cache))

    left = [i for i in need.game_id if i not in cache["sb"] and i not in cache["sum"]][: a.summary_limit]
    print(f"summary fallback: {len(left)} games", file=sys.stderr)
    with ThreadPoolExecutor(a.workers) as ex:
        for k, (gid, b) in enumerate(ex.map(summary_game, left)):
            cache["sum"][gid] = b
            if k % 500 == 499: cp.write_text(json.dumps(cache)); print(f"  {k+1}/{len(left)}", file=sys.stderr)
    cp.write_text(json.dumps(cache))

    def look(gid):
        if gid in cache["sb"]: return cache["sb"][gid], "scoreboard"
        if cache["sum"].get(gid): return cache["sum"][gid], "summary"
        return None, ""
    rows = [look(i) for i in g.game_id]
    g["espn_spread_home"] = [r[0]["home_spread"] if r[0] else None for r in rows]
    g["espn_total"] = [r[0]["total"] if r[0] else None for r in rows]
    g["espn_provider"] = [r[0]["provider"] if r[0] else None for r in rows]
    g["espn_src"] = [r[1] for r in rows]
    g.to_csv(a.out, index=False)
    got = g.loc[g.spread_home.isna(), "espn_spread_home"].notna().sum()
    print(f"ESPN filled {got} of {len(need)} missing lines", file=sys.stderr)


if __name__ == "__main__":
    main()
