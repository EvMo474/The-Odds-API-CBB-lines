#!/usr/bin/env python3
"""
Probe odds APIs for NCAAB halftime lines. Run it WHILE college games are live (ideally a few at halftime).
  export ODDS_API_KEY=...   # the-odds-api.com   (optional)
  export SGO_API_KEY=...    # sportsgameodds.com  (optional)
  python probe.py            # probes whichever keys are set; raw responses saved to probe_out/
It answers: (1) is there a LIVE full-game spread for in-progress games? (2) is there a separate 2nd-half spread?
Credits used: Odds API ~1 + 2 per probed game (max 3 games); SGO 1 request.
"""
import json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
import requests

OUT = Path("probe_out"); OUT.mkdir(exist_ok=True)
OA = os.environ.get("ODDS_API_BASE", "https://api.the-odds-api.com/v4")
SGO = os.environ.get("SGO_BASE", "https://api.sportsgameodds.com/v2")
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

def dump(name, obj):
    p = OUT / f"{stamp}_{name}.json"; p.write_text(json.dumps(obj, indent=1)); return p

def probe_odds_api(key):
    print("\n=== The Odds API ===")
    r = requests.get(f"{OA}/sports/basketball_ncaab/odds", timeout=30,
                     params=dict(apiKey=key, regions="us", markets="spreads,totals", oddsFormat="american"))
    print("status", r.status_code, "| credits used:", r.headers.get("x-requests-used"), "remaining:", r.headers.get("x-requests-remaining"))
    if r.status_code != 200:
        print(r.text[:300]); return
    ev = r.json(); dump("oddsapi_odds", ev)
    now = datetime.now(timezone.utc)
    live = [e for e in ev if datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) < now]
    print(f"{len(ev)} events returned, {len(live)} already started (live or just finished)")
    for e in live[:3]:
        bks = e.get("bookmakers", [])
        sp = [(b["key"], m["outcomes"]) for b in bks for m in b["markets"] if m["key"] == "spreads"]
        print(f"- {e['away_team']} @ {e['home_team']}: {len(bks)} books with live data; "
              f"full-game spread books: {len(sp)}", ("e.g. " + sp[0][0] + " " + str([(o['name'], o.get('point'), o['price']) for o in sp[0][1]])) if sp else "")
        mk = requests.get(f"{OA}/sports/basketball_ncaab/events/{e['id']}/markets", timeout=30, params=dict(apiKey=key, regions="us"))
        if mk.status_code == 200:
            keys = sorted({m["key"] for b in mk.json().get("bookmakers", []) for m in b.get("markets", [])})
            print("  market keys offered:", keys)
            print("  2nd-half spread offered:", "spreads_h2" in keys)
            dump(f"oddsapi_markets_{e['id']}", mk.json())
        else:
            print("  markets endpoint:", mk.status_code, mk.text[:150])
    if not live:
        print("no games in progress right now: rerun during a game")

def probe_sgo(key):
    print("\n=== SportsGameOdds ===")
    r = requests.get(f"{SGO}/events", headers={"x-api-key": key}, timeout=30,
                     params=dict(leagueID="NCAAB", oddsAvailable="true", limit=50))
    print("status", r.status_code)
    if r.status_code != 200:
        print(r.text[:300]); return
    data = r.json().get("data", []); dump("sgo_events", data)
    print(len(data), "events with odds")
    for e in data:
        st = e.get("status") or {}
        started, done = st.get("started"), st.get("completed") or st.get("ended")
        if not started or done:
            continue
        ids = list((e.get("odds") or {}).keys())
        periods = sorted({i.split("-")[2] for i in ids if i.count("-") >= 4})
        sp = [i for i in ids if "-sp-" in i]
        print(f"- {e.get('teams', {}).get('away', {}).get('names', {}).get('long')} @ {e.get('teams', {}).get('home', {}).get('names', {}).get('long')}: "
              f"{len(ids)} odds | periods: {periods} | spread oddIDs: {sp[:6]}")
        game_sp = [i for i in sp if "-game-" in i]
        if game_sp:
            o = e["odds"][game_sp[0]]
            print("  sample live full-game spread:", {k: o.get(k) for k in ("bookSpread", "fairSpread", "bookOdds")})
            print("  per-book entries:", len(o.get("byBookmaker", {}) or {}))
        print("  2nd-half spread offered:", any("-2h-" in i for i in sp))
    print("(status fields on first event:", json.dumps((data[0].get("status") if data else None))[:200], ")")

if __name__ == "__main__":
    ran = False
    if os.environ.get("ODDS_API_KEY"): probe_odds_api(os.environ["ODDS_API_KEY"]); ran = True
    if os.environ.get("SGO_API_KEY"): probe_sgo(os.environ["SGO_API_KEY"]); ran = True
    if not ran: sys.exit("set ODDS_API_KEY and/or SGO_API_KEY")
    print(f"\nraw responses saved in {OUT}/ : send me that folder if anything looks off")
