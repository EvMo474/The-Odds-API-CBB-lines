#!/usr/bin/env python3
"""
CBB halftime logger. Polls ESPN's public scoreboard and, per game, records:
  - the pregame line (first seen while the game is 'pre')
  - at HALFTIME: first-half box score (team totals at the break ARE first-half stats),
    the score, and every odds object ESPN shows (scoreboard + summary 'odds'/'pickcenter'),
    re-sampled each poll through halftime and the first minutes of the 2nd half
  - at FINAL: final score, per-half linescores, full-game box score (2H = final - 1H)
One JSON file per game: data/games/<YYYYMMDD>/<event_id>.json  (merge-friendly in git).

  python logger.py --once              # one poll, then exit
  python logger.py --loop --minutes 295 --push   # poll every 60s for ~5h, commit every 20 min
Env: ESPN_BASE overrides the API base (used for testing).
"""
import argparse, json, os, re, subprocess, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = os.environ.get("ESPN_BASE", "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball")
ET = ZoneInfo("America/New_York")
DATA = Path(os.environ.get("CBB_DATA", "data/games"))
S = requests.Session()
S.headers["User-Agent"] = "cbb-halftime-logger/1.0"


def get(url, params=None):
    for i in range(3):
        try:
            r = S.get(url, params=params, timeout=25)
            if r.status_code == 200:
                return r.json()
            print(f"  HTTP {r.status_code} {url}", file=sys.stderr)
        except Exception as e:
            print(f"  fetch error {e}", file=sys.stderr)
        time.sleep(2 * (i + 1))
    return None


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(p):
    return json.loads(p.read_text()) if p.exists() else None


def save(p, d):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=1, sort_keys=True))


# ---------- parsing ----------
def parse_odds(odds_list, home_abbr, away_abbr):
    """Return list of {provider, home_spread (neg = home favored), total, details, raw_spread}."""
    out = []
    for o in odds_list or []:
        det = o.get("details") or ""
        hs = None
        m = re.match(r"\s*([A-Za-z0-9&.\-' ]+?)\s+([+-]?\d+(?:\.\d+)?)\s*$", det)
        if m:
            fav, num = m.group(1).strip().upper(), float(m.group(2))
            if fav == (home_abbr or "").upper():
                hs = num
            elif fav == (away_abbr or "").upper():
                hs = -num
        if hs is None and "EVEN" in det.upper():
            hs = 0.0
        if hs is None and o.get("spread") is not None:      # fallback: needs favorite flag
            sp = abs(float(o["spread"]))
            if (o.get("homeTeamOdds") or {}).get("favorite"):
                hs = -sp
            elif (o.get("awayTeamOdds") or {}).get("favorite"):
                hs = sp
        out.append({"provider": (o.get("provider") or {}).get("name"), "home_spread": hs,
                    "total": o.get("overUnder"), "details": det, "raw_spread": o.get("spread")})
    return out


def pair(s):
    """'22-50' -> (22, 50)"""
    m = re.match(r"\s*(\d+)\s*-\s*(\d+)", str(s or ""))
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def team_stats(stat_list):
    d = {}
    for s in stat_list or []:
        n, v = s.get("name", ""), s.get("displayValue")
        if n.startswith("fieldGoalsMade-"):
            d["fgm"], d["fga"] = pair(v)
        elif n.startswith("threePointFieldGoalsMade-"):
            d["3pm"], d["3pa"] = pair(v)
        elif n.startswith("freeThrowsMade-"):
            d["ftm"], d["fta"] = pair(v)
    if d.get("fga"):
        d["fg_pct"] = round(100 * d["fgm"] / d["fga"], 2)
    return d


def box(summary):
    """{'home': stats, 'away': stats} from a summary payload."""
    res = {}
    comp = ((summary.get("header") or {}).get("competitions") or [{}])[0]
    ha = {str(c.get("team", {}).get("id")): c.get("homeAway") for c in comp.get("competitors", [])}
    for t in (summary.get("boxscore") or {}).get("teams", []):
        side = t.get("homeAway") or ha.get(str(t.get("team", {}).get("id")))
        if side:
            res[side] = team_stats(t.get("statistics"))
    return res


# ---------- per-game update ----------
def handle(ev, date_key):
    cid = ev["id"]
    comp = ev["competitions"][0]
    st = comp["status"]["type"]["name"]            # STATUS_SCHEDULED / IN_PROGRESS / HALFTIME / FINAL
    per, clock = comp["status"].get("period"), comp["status"].get("displayClock")
    cs = {c["homeAway"]: c for c in comp["competitors"]}
    h, a = cs["home"], cs["away"]
    habbr, aabbr = h["team"].get("abbreviation"), a["team"].get("abbreviation")
    path = DATA / date_key / f"{cid}.json"
    g = load(path) or {"event_id": cid, "date_key": date_key, "start": ev.get("date"),
                       "home": {"id": h["team"]["id"], "name": h["team"].get("displayName"), "abbr": habbr},
                       "away": {"id": a["team"]["id"], "name": a["team"].get("displayName"), "abbr": aabbr},
                       "neutral_site": comp.get("neutralSite"), "odds_ticks": []}
    changed = False
    odds = parse_odds(comp.get("odds"), habbr, aabbr)
    sc = {"home": h.get("score"), "away": a.get("score")}

    if st == "STATUS_SCHEDULED" and odds and "pregame" not in g:
        g["pregame"] = {"t": now_iso(), "odds": odds}; changed = True

    if st == "STATUS_HALFTIME":
        if "halftime" not in g:
            summ = get(f"{BASE}/summary", {"event": cid})
            if summ:
                g["halftime"] = {"t": now_iso(), "score": sc, "box_1h": box(summ),
                                 "summary_odds": parse_odds(summ.get("odds"), habbr, aabbr),
                                 "summary_pickcenter": parse_odds(summ.get("pickcenter"), habbr, aabbr),
                                 "raw_summary_odds": summ.get("odds"), "raw_pickcenter": summ.get("pickcenter")}
                changed = True
        # sample the scoreboard odds every poll while at the break
        if len(g["odds_ticks"]) < 60:
            g["odds_ticks"].append({"t": now_iso(), "status": st, "period": per, "clock": clock, "score": sc, "odds": odds, "raw": comp.get("odds")})
            changed = True
    elif st == "STATUS_IN_PROGRESS" and per == 2 and "halftime" in g and len(g["odds_ticks"]) < 70:
        # first few minutes of the 2nd half: does the line move / stay live?
        g["odds_ticks"].append({"t": now_iso(), "status": st, "period": per, "clock": clock, "score": sc, "odds": odds}); changed = True

    if st == "STATUS_FINAL" and "final" not in g:
        summ = get(f"{BASE}/summary", {"event": cid})
        ls = {s: [x.get("displayValue") for x in (cs[s].get("linescores") or [])] for s in ("home", "away")}
        g["final"] = {"t": now_iso(), "score": sc, "linescores": ls,
                      "periods": max(len(ls["home"]), len(ls["away"])),
                      "box_full": box(summ) if summ else None}
        changed = True
    if changed:
        save(path, g)
    return st, changed


def poll():
    now = datetime.now(ET)
    days = {now.strftime("%Y%m%d"), (now - timedelta(days=1)).strftime("%Y%m%d")}
    n = ch = 0
    live = False
    for d in sorted(days):
        sb = get(f"{BASE}/scoreboard", {"dates": d, "groups": 50, "limit": 400})
        if not sb:
            continue
        for ev in sb.get("events", []):
            try:
                st, c = handle(ev, d)
            except Exception as e:
                print(f"  game {ev.get('id')} failed: {e!r}", file=sys.stderr)
                continue
            n += 1; ch += c
            live |= st in ("STATUS_IN_PROGRESS", "STATUS_HALFTIME")
    print(f"{now:%H:%M:%S} games={n} updated={ch} live={live}", flush=True)
    return live


def push():
    cmds = [["git", "add", "data"], ["git", "-c", "user.name=cbb-logger", "-c", "user.email=cbb-logger@users.noreply.github.com",
            "commit", "-m", f"log {now_iso()}"], ["git", "pull", "--rebase", "--autostash"], ["git", "push"]]
    for c in cmds:
        r = subprocess.run(c, capture_output=True, text=True)
        if r.returncode and c[1] != "-c":      # commit with nothing to commit is fine
            print("  git:", " ".join(c[:2]), r.stderr.strip()[:200], file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--minutes", type=float, default=295)
    ap.add_argument("--every", type=int, default=180, help="seconds between polls")
    ap.add_argument("--push", action="store_true", help="git commit+push every 20 min and at the end")
    a = ap.parse_args()
    if a.once or not a.loop:
        poll(); return
    end, last_push = time.time() + a.minutes * 60, time.time()
    while time.time() < end:
        t0 = time.time()
        try:
            poll()
        except Exception as e:
            print("poll error", repr(e), file=sys.stderr)
        if a.push and time.time() - last_push > 1200:
            push(); last_push = time.time()
        time.sleep(max(1, a.every - (time.time() - t0)))
    if a.push:
        push()


if __name__ == "__main__":
    main()
