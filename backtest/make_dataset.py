#!/usr/bin/env python3
"""
One command builds the 5-season, all-conference backtest table (no halftime lines yet).

  python backtest/make_dataset.py --seasons 2022-2026 --out backtest/out

Season = ending year (2022 = 2021-22).  2021 (the 2020-21 COVID season) is intentionally not included.
Steps: hoopR play-by-play -> first-half/second-half shooting per game  (build_first_half.py)
       + pregame closing lines from the open zachmayer dataset, through Apr 2024  (join_spreads.py)
       + ESPN pregame lines for whatever is still missing                         (espn_odds.py)
       + conference of both teams, per season                                    (tag_conferences.py functions)
       + split columns: month, conference vs non-conference, neutral, game type, conference tier, bucket, cold-team margins
"""
import argparse, subprocess, sys
from pathlib import Path
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

MAJOR = r"\bbig ten\b|\bsec\b|southeastern|\bacc\b|atlantic coast|\bbig 12\b|\bbig east\b|\bpac[- ]?12\b"
MID = r"mountain west|atlantic 10|\ba-?10\b|west coast|\bwcc\b|american athletic|\baac\b|missouri valley|\bmvc\b"


def run(cmd):
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run([sys.executable, *map(str, cmd)], check=True)


def tier(conf):
    c = str(conf).lower()
    if conf is None or c in ("nan", "none"): return "unknown"
    import re
    if re.search(MAJOR, c): return "major"
    if re.search(MID, c): return "mid"
    return "low"


def derive(d):
    d = d.copy()
    t = pd.to_datetime(d.date, utc=True, errors="coerce").dt.tz_convert("America/New_York")
    d["game_date_et"] = t.dt.strftime("%Y-%m-%d")
    d["month"] = t.dt.month
    d["month_name"] = t.dt.strftime("%b")
    d["conf_game"] = np.where(d.conference_competition.astype(str).str.lower().isin(["true", "1", "1.0"]), "Conference", "Non-conference")
    d["site"] = np.where(d.neutral_site.astype(str).str.lower().isin(["true", "1", "1.0"]), "Neutral", "Home/Road")
    tt = d.type_abbreviation.astype(str)
    d["game_type"] = np.select(
        [tt.eq("TRNMNT"), d.season_type.eq(3) & ~tt.eq("TRNMNT"), tt.isin(["RD64", "RD32", "RD128", "SIXTEEN", "ELEVEN", "ELITE", "FINAL4", "FINAL", "SEMI", "QUAL", "QUALIFY"])],
        ["Conf tournament", "Postseason", "Postseason"], default="Regular")
    d["home_tier"] = d.home_conf.map(tier); d["away_tier"] = d.away_conf.map(tier)
    order = {"major": 0, "mid": 1, "low": 2, "unknown": 3}
    d["matchup_tier"] = [" vs ".join(sorted([h, a], key=order.get)) for h, a in zip(d.home_tier, d.away_tier)]
    # fill the pregame line: zachmayer first, ESPN second
    if "espn_spread_home" in d:
        d["line_src"] = np.where(d.spread_home.notna(), "covers", np.where(d.espn_spread_home.notna(), "espn:" + d.espn_provider.fillna("?"), ""))
        d["spread_home"] = d.spread_home.fillna(d.espn_spread_home)
        d["total"] = d.total.fillna(d.espn_total)
    d["spread_away"] = -d.spread_home
    # cold team = the side with the worse first-half FG%
    ca = d.away_fg_gap_1h < 0
    d["cold_side"] = np.where(ca, "away", "home")
    d["gap"] = d.away_fg_gap_1h.abs()
    d["cold_team"] = np.where(ca, d.away_name, d.home_name)
    d["cold_conf"] = np.where(ca, d.away_conf, d.home_conf)
    d["cold_margin_1h"] = np.where(ca, d.ht_margin_away, -d.ht_margin_away)
    d["cold_margin_2h"] = np.where(ca, d.h2_margin_away, -d.h2_margin_away)
    d["cold_margin_final"] = d.cold_margin_1h + d.cold_margin_2h
    d["cold_spread"] = np.where(ca, d.spread_away, d.spread_home)          # + = cold team is the dog
    d["bucket"] = np.select([d.cold_spread.isna(), d.cold_spread == 0, d.cold_spread > 0], ["", "Pick",
                            np.where(ca, "Road Dog", "Home Dog")], np.where(ca, "Road Fav", "Home Fav"))
    d.loc[d.cold_spread.isna(), "bucket"] = ""
    # no-halftime-line proxy: did the cold team beat half of its closing spread in the 2nd half?  > 0 = yes
    d["h2_vs_half_close"] = d.cold_margin_2h + d.cold_spread / 2
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2022-2026"); ap.add_argument("--out", default="backtest/out")
    ap.add_argument("--work", default="backtest_work"); ap.add_argument("--summary-limit", default="20000")
    a = ap.parse_args()
    out, work = Path(a.out), Path(a.work); out.mkdir(parents=True, exist_ok=True); work.mkdir(parents=True, exist_ok=True)
    run([HERE / "build_first_half.py", "--seasons", a.seasons, "--cache", work / "pbp_cache", "--out", work / "first_half.csv"])
    run([HERE / "join_spreads.py", work / "first_half.csv", "--out", work / "with_lines.csv"])
    run([HERE / "espn_odds.py", work / "with_lines.csv", "--out", work / "with_espn.csv", "--cache", work / "espn_odds_cache.json",
         "--summary-limit", a.summary_limit])
    import tag_conferences as tc
    g = pd.read_csv(work / "with_espn.csv", low_memory=False)
    conf = pd.concat([tc.team_conf(tc.load_standings(s, work / "standings_cache")) for s in sorted(g.season.unique())], ignore_index=True)
    g = tc.tag(g, conf, ["big ten"])                                  # adds home_conf / away_conf (target columns unused)
    g = g.drop(columns=[c for c in ("home_target", "away_target", "both_target") if c in g])
    g = derive(g)
    g.to_csv(out / "cbb_backtest_games.csv.gz", index=False, compression="gzip")

    lines = [f"games {len(g)}", f"seasons {sorted(g.season.unique())}"]
    lines.append("\nper season: games / with pregame line / missing a conference label")
    lines.append(g.groupby("season").agg(games=("game_id", "size"), lined=("spread_home", lambda s: int(s.notna().sum())),
                                          no_conf=("home_conf", lambda s: int(s.isna().sum()))).to_string())
    lines.append("\nline source:\n" + g.line_src.value_counts().to_string() if "line_src" in g else "")
    q = g[(g.gap >= 10) & g.spread_home.notna() & (g.pbp_complete_1h == True)]
    lines.append(f"\ngap>=10 with line: {len(q)}")
    lines.append("\nby bucket:\n" + q.bucket.value_counts().to_string())
    lines.append("\nby conference tier matchup:\n" + q.matchup_tier.value_counts().to_string())
    lines.append("\nconferences (cold team) with games at gap>=10 and a line:\n" + q.cold_conf.value_counts().to_string())
    (out / "coverage.txt").write_text("\n".join(lines)); print("\n".join(lines))


if __name__ == "__main__":
    main()
