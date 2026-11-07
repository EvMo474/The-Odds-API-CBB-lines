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
import argparse, difflib, json, os, re, subprocess, sys, time
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
    # ESPN sometimes flags the break late: the game sits at "period 1, 0:00" (or END_PERIOD) before HALFTIME appears,
    # so those count as the break too.  The raw status is kept in g["break_via"] to see how often that happens.
    brk = st == "STATUS_HALFTIME" or (per == 1 and (st == "STATUS_END_PERIOD" or (st == "STATUS_IN_PROGRESS" and str(clock).strip() in ("0:00", "0.0", "0"))))
    STATUS_MAP[cid] = {"status": "HALFTIME" if brk else st.replace("STATUS_", ""), "period": per, "clock": clock}
    odds = parse_odds(comp.get("odds"), habbr, aabbr)
    sc = {"home": h.get("score"), "away": a.get("score")}

    if st == "STATUS_SCHEDULED" and odds and "pregame" not in g:
        g["pregame"] = {"t": now_iso(), "odds": odds}; changed = True
    if st == "STATUS_SCHEDULED" and ev.get("date"):
        try:
            mins = (datetime.fromisoformat(ev["date"].replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds() / 60
            if -2 <= mins <= 6:
                PRE.append((g, path))
        except ValueError:
            pass

    if brk:
        if "halftime_first_seen" not in g:
            g["halftime_first_seen"] = now_iso(); g["break_via"] = st; changed = True
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
    return ("STATUS_HALFTIME" if brk else st), changed, (g, path, comp, habbr, aabbr) if brk else None


ODDS_BASE = os.environ.get("ODDS_API_BASE", "https://api.the-odds-api.com/v4")
SKIP_BOOKS = {"bovada"}                                  # books Evan cannot bet
REGIONS = os.environ.get("ODDS_REGIONS", "us")          # one region = every US book (DK, FD, BetRivers, ...) for the same credits
MARKETS = os.environ.get("ODDS_MARKETS", "spreads,totals")
GAP_FLAG = float(os.environ.get("FLAG_GAP", "10"))       # cold-team FG% gap that qualifies
WIN_ZONE = float(os.environ.get("WIN_ZONE", "2"))        # cold team within this many points of winning the game at the live line
STATUS_MAP = {}                                          # latest ESPN status per game, for the live board
PRE = []                                                 # scheduled games about to tip (filled by handle, used by poll)


def _norm(x):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", str(x).lower().replace("&", " and ").replace("-", " ")).split())


def _match(g, events):
    hn, an = _norm(g["home"]["name"]), _norm(g["away"]["name"])
    best, score = None, 0
    for e in events:
        sc = (difflib.SequenceMatcher(None, hn, _norm(e["home_team"])).ratio() +
              difflib.SequenceMatcher(None, an, _norm(e["away_team"])).ratio()) / 2
        if sc > score:
            best, score = e, sc
    return best, score


def book_lines(event):
    """{book: {'spread': home spread (neg = home favored), 'total': points}} for every book on the event."""
    out = {}
    for b in event.get("bookmakers", []):
        if b.get("key") in SKIP_BOOKS:
            continue
        d = {}
        for m in b.get("markets", []):
            for o in m.get("outcomes", []):
                if m["key"] == "spreads" and o["name"] == event["home_team"]:
                    d["spread"] = o.get("point")
                elif m["key"] == "totals" and str(o.get("name")).lower() == "over":
                    d["total"] = o.get("point")
        if d:
            out[b["key"]] = d
    return out


def fetch_odds(key):
    try:
        r = S.get(f"{ODDS_BASE}/sports/basketball_ncaab/odds", timeout=30,
                  params=dict(apiKey=key, regions=REGIONS, markets=MARKETS, oddsFormat="american"))
        if r.status_code != 200:
            print(f"  odds api HTTP {r.status_code} {r.text[:120]}", file=sys.stderr); return None, None
        return r.json(), r.headers.get("x-requests-remaining")
    except Exception as e:
        print(f"  odds api error {e!r}", file=sys.stderr); return None, None


def fallback_event(key, g):
    """Game missing from the bulk odds list: is it in the (free) events list, and does a per-event call return lines?"""
    info = {}
    try:
        r = S.get(f"{ODDS_BASE}/sports/basketball_ncaab/events", timeout=30, params=dict(apiKey=key))
        evs = r.json() if r.status_code == 200 else []
        best, score = _match(g, evs)
        info["events_list_n"] = len(evs)
        info["events_list_match"] = round(score, 2)
        if best is not None and score >= 0.82:
            r2 = S.get(f"{ODDS_BASE}/sports/basketball_ncaab/events/{best['id']}/odds", timeout=30,
                       params=dict(apiKey=key, regions=REGIONS, markets=MARKETS, oddsFormat="american"))
            info["event_odds_http"] = r2.status_code
            if r2.status_code == 200:
                return r2.json(), info
    except Exception as e:
        info["fallback_error"] = repr(e)
    return None, info


def put_lines(tick, event):
    bl = book_lines(event)
    tick["books"] = bl
    for bk in ("draftkings", "fanduel"):          # flat keys kept for report.py
        if bk in bl and "spread" in bl[bk]:
            tick[bk + "_home_spread"] = bl[bk]["spread"]
        if bk in bl and "total" in bl[bk]:
            tick[bk + "_total"] = bl[bk]["total"]
    tick["odds_event_id"] = event["id"]


def pre_home_spread(g):
    """Home spread used for the bucket: DraftKings at tip if we have it, else any book at tip, else ESPN's pregame DK line."""
    cb = {k: v.get("spread") for k, v in ((g.get("closing") or {}).get("books") or {}).items() if v.get("spread") is not None}
    if cb:
        return cb.get("draftkings", next(iter(cb.values()))), "close"
    po = (g.get("pregame") or {}).get("odds") or []
    if po and po[0].get("home_spread") is not None:
        return po[0]["home_spread"], "pregame"
    return None, None


def compute_flag(g):
    """Cold team (lower 1H FG%), its bucket from the PREGAME/closing line, and its implied 2H line at every book."""
    h = g.get("halftime")
    ticks = [x for x in g.get("odds_api_ticks", []) if x.get("books")]
    if not h or not ticks:
        return
    b, sc = h["box_1h"], h["score"]
    if "home" not in b or "away" not in b:
        return
    fh, fa = b["home"].get("fg_pct"), b["away"].get("fg_pct")
    if fh is None or fa is None:
        return
    cold_away = fa < fh
    hm = int(sc["home"]) - int(sc["away"])                       # home halftime margin
    mc = -hm if cold_away else hm                                # cold team's halftime margin
    gap = round(abs(fa - fh), 1)
    lines, live_home, cold_full = {}, {}, {}
    for bk, d in ticks[-1]["books"].items():
        if d.get("spread") is not None:
            live_home[bk] = d["spread"]
            cold_full[bk] = -d["spread"] if cold_away else d["spread"]
            cs = -d["spread"] if cold_away else d["spread"]       # cold team's full-game spread
            lines[bk] = round(cs + mc, 1)                          # its second-half spread (+ = underdog)
    if not lines:
        return
    best_bk = max(lines, key=lines.get)
    pre, src = pre_home_spread(g)
    bucket = None
    if pre is not None:
        cold_pre = -pre if cold_away else pre                      # cold team's pregame spread (+ = underdog)
        bucket = ("Road" if cold_away else "Home") + (" Dog" if cold_pre > 0 else " Fav" if cold_pre < 0 else " Pick")
    # sanity: live home spread should be near pre/2 - home margin; if the swapped orientation fits far better, the home/away labels disagree
    suspect = False
    if pre is not None and live_home:
        obs = sorted(live_home.values())[len(live_home) // 2]
        exp_ok, exp_sw = pre * 0.5 - hm, pre * 0.5 + hm
        suspect = abs(obs - exp_ok) > 12 and abs(obs - exp_sw) + 8 < abs(obs - exp_ok)
    g["flag"] = {"t": ticks[-1]["t"], "cold": "away" if cold_away else "home", "gap": gap, "cold_margin_1h": mc,
                 "bucket": bucket, "bucket_src": src, "pre_home_spread": pre, "neutral": bool(g.get("neutral_site")),
                 "suspect": suspect, "lines_2h": lines, "best_book": best_bk, "best_line": lines[best_bk],
                 "qualifies": bool(bucket == "Road Dog" and gap >= GAP_FLAG and not suspect), "gap_min": GAP_FLAG}
    # win zone: the live full-game line has the cold team within WIN_ZONE points of winning outright, so covering ~= winning
    cf = sorted(cold_full.values())
    med = cf[len(cf) // 2] if len(cf) % 2 else (cf[len(cf) // 2 - 1] + cf[len(cf) // 2]) / 2
    g["flag"].update({"cold_full_spread": round(med, 1), "cold_full_spread_by_book": cold_full,
                      "win_zone": bool(med <= WIN_ZONE and gap >= GAP_FLAG and not suspect), "win_zone_max": WIN_ZONE})


def odds_api_poll(halftime_games, pre_games):
    """One bulk call (spreads+totals, all US books) per poll when games are at the break or about to tip."""
    key = os.environ.get("ODDS_API_KEY")
    if not key or not (halftime_games or pre_games):
        return
    events, left = fetch_odds(key)
    if events is None:
        return
    for g, path, comp, habbr, aabbr in halftime_games:
        best, score = _match(g, events)
        tick = {"t": now_iso(), "match": round(score, 2), "credits_left": left, "n_events": len(events)}
        if best is not None and score >= 0.82:
            put_lines(tick, best)
        else:
            tick["unmatched"] = True
            if best is not None:
                tick["closest"] = f"{best['away_team']} @ {best['home_team']}"
            ev2, info = fallback_event(key, g)
            tick.update(info)
            if ev2:
                put_lines(tick, ev2); tick["unmatched"] = False; tick["via"] = "event_odds"
        g.setdefault("odds_api_ticks", []).append(tick)
        compute_flag(g)
        if len(g["odds_api_ticks"]) <= 40:
            save(path, g)
    for g, path in pre_games:                       # last snapshot before tip = closing line, every book
        best, score = _match(g, events)
        if best is not None and score >= 0.82:
            c = {"t": now_iso(), "odds_event_id": best["id"], "books": book_lines(best)}
            g["closing"] = c
            save(path, g)


def poll():
    now = datetime.now(ET)
    days = {now.strftime("%Y%m%d"), (now - timedelta(days=1)).strftime("%Y%m%d")}
    n = ch = 0
    live = False
    at_half = []
    for d in sorted(days):
        sb = get(f"{BASE}/scoreboard", {"dates": d, "groups": 50, "limit": 400})
        if not sb:
            continue
        for ev in sb.get("events", []):
            try:
                st, c, hh = handle(ev, d)
            except Exception as e:
                print(f"  game {ev.get('id')} failed: {e!r}", file=sys.stderr)
                continue
            n += 1; ch += c
            if hh: at_half.append(hh)
            live |= st in ("STATUS_IN_PROGRESS", "STATUS_HALFTIME")
    odds_api_poll(at_half, list(PRE)); PRE.clear()
    print(f"{now:%H:%M:%S} games={n} updated={ch} live={live} at_halftime={len(at_half)}", flush=True)
    return live


GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="cbb-logger", GIT_AUTHOR_EMAIL="cbb-logger@users.noreply.github.com",
               GIT_COMMITTER_NAME="cbb-logger", GIT_COMMITTER_EMAIL="cbb-logger@users.noreply.github.com")


def git(*args):
    r = subprocess.run(["git", *args], capture_output=True, text=True, env=GIT_ENV)
    if r.returncode:
        print("  git:", " ".join(args[:2]), (r.stderr or r.stdout).strip()[:200], file=sys.stderr)
    return r.returncode == 0


def push():
    """Commit data, sync with origin/main, push. Never leaves the repo mid-rebase or detached."""
    try:
        import livejson
        livejson.write(str(DATA), str(DATA.parent / "live.json"), STATUS_MAP)
    except Exception as e:
        print("  live.json skipped:", repr(e), file=sys.stderr)
    git("add", "data")
    subprocess.run(["git", "commit", "-m", f"log {now_iso()}"], capture_output=True, text=True, env=GIT_ENV)
    if not git("pull", "--rebase", "--autostash", "origin", "main"):
        subprocess.run(["git", "rebase", "--abort"], capture_output=True, text=True, env=GIT_ENV)
        git("pull", "--no-rebase", "--no-edit", "-X", "ours", "origin", "main")   # data files are per-game, so this is safe
    if not git("push", "origin", "HEAD:main"):
        git("pull", "--no-rebase", "--no-edit", "-X", "ours", "origin", "main")
        git("push", "origin", "HEAD:main")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--minutes", type=float, default=295)
    ap.add_argument("--every", type=int, default=180, help="seconds between polls")
    ap.add_argument("--push", action="store_true", help="git commit+push periodically and at the end")
    ap.add_argument("--push-every", type=int, default=300, help="seconds between pushes (default 300 = 5 min)")
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
        if a.push and time.time() - last_push > a.push_every:
            push(); last_push = time.time()
        time.sleep(max(1, a.every - (time.time() - t0)))
    if a.push:
        push()


if __name__ == "__main__":
    main()
