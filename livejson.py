"""Builds data/live.json, the file the GitHub Pages board reads.

Rows = every game that reached halftime in the last few days (newest first, flagged on top), with the cold team,
its pregame/closing bucket, its second-half line at each book, and the result once the game is final.
`season` = running record for every day in the repo.  Never raises: a failure here must not stop the logger.
"""
import json, statistics as st
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
NAMES = {"draftkings": "DraftKings", "fanduel": "FanDuel", "betrivers": "BetRivers", "betmgm": "BetMGM",
         "williamhill_us": "Caesars", "fanatics": "Fanatics"}
SKIP = {"bovada"}
GAP = 10.0
WIN_ZONE = 2.0
SHOW_DAYS = 3


def _t(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _fmt(d):
    return d.astimezone(ET).strftime("%-I:%M%p").lower()


def _row(g, status):
    h = g.get("halftime")
    if not h:
        return None
    fin = g.get("final")
    seen = g.get("halftime_first_seen")
    ticks = g.get("odds_api_ticks") or []
    target = _t(seen) + timedelta(minutes=5) if seen else None
    cand = []
    for x in ticks:
        bk = {k: v.get("spread") for k, v in (x.get("books") or {}).items() if v.get("spread") is not None and k not in SKIP}
        if not bk:
            bk = {k: x[k + "_home_spread"] for k in ("draftkings", "fanduel") if x.get(k + "_home_spread") is not None}
        if bk:
            cand.append((abs((_t(x["t"]) - target).total_seconds()) if target else 0, bk))
    books = min(cand, key=lambda c: c[0])[1] if cand else {}
    a, hm_ = g["away"], g["home"]
    b, sc = h["box_1h"], h["score"]
    fa, fh = b["away"]["fg_pct"], b["home"]["fg_pct"]
    ah, hh = int(sc["away"]), int(sc["home"])
    ca = fa < fh
    hmar = hh - ah
    mc = -hmar if ca else hmar
    gap = round(abs(fa - fh), 1)
    l2 = {k: round((-v if ca else v) + mc, 1) for k, v in books.items()}
    cs = {k: (-v if ca else v) for k, v in books.items()}

    cb = {k: v.get("spread") for k, v in ((g.get("closing") or {}).get("books") or {}).items() if v.get("spread") is not None and k not in SKIP}
    pre, src = None, None
    if cb:
        pre, src = cb.get("draftkings", next(iter(cb.values()))), "close"
    else:
        po = (g.get("pregame") or {}).get("odds") or []
        if po and po[0].get("home_spread") is not None:
            pre, src = po[0]["home_spread"], "pregame"

    suspect = False
    if pre is not None and books:
        obs = sorted(books.values())[len(books) // 2]
        ok_, sw = pre * 0.5 - hmar, pre * 0.5 + hmar
        suspect = abs(obs - ok_) > 12 and abs(obs - sw) + 8 < abs(obs - ok_)

    bucket, close_txt, cp = "", "", None
    if pre is not None:
        cp = -pre if ca else pre
        bucket = ("Road" if ca else "Home") + (" Dog" if cp > 0 else " Fav" if cp < 0 else " Pick")
        fav = hm_ if pre < 0 else a
        close_txt = ("Pick'em" if pre == 0 else f"{fav['name']} -{abs(pre):g}")
    best = ""
    if l2:
        bk = max(l2, key=l2.get)
        best = f"{l2[bk]:+.1f} {NAMES.get(bk, bk)}"
    qual = bool(bucket == "Road Dog" and gap >= GAP and l2 and not suspect)
    cs_med = st.median(cs.values()) if cs else None
    zone = bool(cs_med is not None and cs_med <= WIN_ZONE and gap >= GAP and not suspect)

    # market check: if the market just halves the closing line and adds the first-half margin, the implied 2H line
    # (live cold spread + cold 1H margin) equals half the closing cold spread.  dev = implied 2H line - that.
    exp2 = round(cp / 2, 1) if cp is not None else None
    dev = round(cs_med + mc - cp / 2, 1) if (cp is not None and cs_med is not None and not suspect) else None
    m2, res, won = None, {}, ""
    if fin and not suspect:
        fs = fin["score"]
        fmh = int(fs["home"]) - int(fs["away"])
        fm = -fmh if ca else fmh
        m2 = fm - mc
        for k, l in l2.items():
            x = m2 + l
            res[k] = "W" if x > 0 else "L" if x < 0 else "P"
        won = "W" if fm > 0 else "L" if fm < 0 else "T"
    return dict(
        id=g["event_id"], day=g["date_key"], ht=_fmt(_t(seen)) if seen else "", seen=seen or "",
        game=f"{a['name']} @ {hm_['name']}", neutral=bool(g.get("neutral_site")),
        status=status.get("status") if status else ("FINAL" if fin else ""),
        period=status.get("period") if status else None, clock=status.get("clock") if status else None,
        close=close_txt, close_src=src or "", bucket=bucket, half=f"{ah}-{hh}", cold=(a if ca else hm_)["name"],
        gap=gap, l2={NAMES.get(k, k): v for k, v in l2.items()}, best=best, live_cold=(round(cs_med, 1) if cs_med is not None else None),
        flag=qual, zone=zone, suspect=suspect, m2=m2, res={NAMES.get(k, k): v for k, v in res.items()}, won=won,
        exp2=exp2, dev=dev, final=(f"{fin['score']['away']}-{fin['score']['home']}" if fin else ""), ot=bool(fin and fin.get("periods", 2) > 2),
        _res_dk=res.get("draftkings"), _res_fd=res.get("fanduel"))


def build(data_dir, status_map=None, now=None):
    now = now or datetime.now(timezone.utc)
    data_dir = Path(data_dir)
    days = sorted(p.name for p in data_dir.iterdir() if p.is_dir())
    recent = set(days[-SHOW_DAYS:])
    rows, counts = [], {"games": 0, "halftime": 0, "final": 0}
    season = {}
    for d in days:
        for p in data_dir.joinpath(d).glob("*.json"):
            try:
                g = json.loads(p.read_text())
                r = _row(g, (status_map or {}).get(g["event_id"]))
            except Exception:
                continue
            if d in recent:
                counts["games"] += 1
                counts["final"] += bool(g.get("final"))
            if not r:
                continue
            if d in recent:
                counts["halftime"] += 1
                rows.append(r)
            if r["bucket"] and not r["suspect"]:
                for tier, ok in (("gap10", r["gap"] >= GAP), ("all", True)):
                    if not ok:
                        continue
                    for bk in ("DraftKings", "FanDuel"):
                        z = r["res"].get(bk)
                        if z:
                            s = season.setdefault(f"{r['bucket']}|{tier}|{bk}", {"W": 0, "L": 0, "P": 0})
                            s[z] += 1
    rows.sort(key=lambda r: r["seen"], reverse=True)
    rows.sort(key=lambda r: not r["flag"])
    for r in rows:
        r.pop("_res_dk", None); r.pop("_res_fd", None)
    return {"as_of": now.isoformat(timespec="seconds"), "gap_min": GAP, "win_zone": WIN_ZONE, "counts": counts,
            "rows": rows, "season": season}


def write(data_dir="data/games", out="data/live.json", status_map=None):
    try:
        doc = build(data_dir, status_map)
        Path(out).write_text(json.dumps(doc, separators=(",", ":"), ensure_ascii=False))
        return True
    except Exception as e:                      # never stop the logger over the board
        print("  live.json failed:", repr(e))
        return False
