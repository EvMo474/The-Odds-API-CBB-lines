#!/usr/bin/env python3
"""
Tag each game with the conference of both teams (per season, so realignment is handled) and flag games
where both teams are in your chosen conferences.

  pip install pandas pyarrow requests
  python3 tag_conferences.py cbb_with_lines.csv --out cbb_with_lines_conf.csv
  python3 tag_conferences.py cbb_with_lines.csv --show        # list conference names found, to verify matching
  python3 tag_conferences.py cbb_with_lines.csv --confs "big ten,sec,acc,big 12,big east,pac-12"

Source: hoopR-mbb-data standings parquet (season, group_name, team_id). Adds columns:
  home_conf, away_conf, home_target, away_target, both_target, same_conf
"""
import argparse, re, sys
from pathlib import Path
import pandas as pd

URL = "https://github.com/sportsdataverse/hoopR-mbb-data/raw/main/mbb/standings/parquet/standings_{season}.parquet"
PATTERNS = {   # key -> regex on lower-cased group_name
    "big ten": r"\bbig ten\b", "sec": r"\bsec\b|southeastern", "acc": r"\bacc\b|atlantic coast",
    "big 12": r"\bbig 12\b", "big east": r"\bbig east\b", "pac-12": r"\bpac[- ]?12\b",
    "mountain west": r"mountain west", "a-10": r"atlantic 10|\ba-?10\b", "wcc": r"west coast|\bwcc\b",
    "american": r"\bamerican athletic\b|\baac\b",
}
GENERIC = re.compile(r"ncaa|division|^all\b|independent", re.I)


def load_standings(season, cache):
    import requests
    p = cache / f"standings_{season}.parquet"
    if not p.exists() or p.stat().st_size == 0:
        cache.mkdir(parents=True, exist_ok=True)
        print(f"downloading standings {season}", file=sys.stderr)
        r = requests.get(URL.format(season=season), timeout=120, headers={"User-Agent": "cbb-conf/1.0"})
        r.raise_for_status()
        p.write_bytes(r.content)
    return pd.read_parquet(p, columns=["season", "group_name", "team_id"])


def team_conf(st):
    """one conference per (season, team_id): ignore generic groups, keep first remaining."""
    st = st.drop_duplicates()
    st = st[~st.group_name.fillna("").str.contains(GENERIC)]
    return st.groupby(["season", "team_id"], as_index=False).group_name.first().rename(columns={"group_name": "conf"})


def tag(games, tc, wanted):
    pats = [PATTERNS.get(w.strip().lower(), re.escape(w.strip().lower())) for w in wanted]
    rx = re.compile("|".join(pats))
    m = tc.copy(); m["target"] = m.conf.str.lower().str.contains(rx)
    out = games
    for side in ("home", "away"):
        mm = m.rename(columns={"team_id": f"{side}_id", "conf": f"{side}_conf", "target": f"{side}_target"})
        out = out.merge(mm, on=["season", f"{side}_id"], how="left")
    out["home_target"] = out.home_target.fillna(False).astype(bool)
    out["away_target"] = out.away_target.fillna(False).astype(bool)
    out["both_target"] = out.home_target & out.away_target
    out["same_conf"] = (out.home_conf == out.away_conf) & out.home_conf.notna()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("games_csv"); ap.add_argument("--out", default="cbb_with_lines_conf.csv")
    ap.add_argument("--cache", default="standings_cache")
    ap.add_argument("--confs", default="big ten,sec,acc,big 12,big east,pac-12")
    ap.add_argument("--show", action="store_true", help="list conference names found per season and exit")
    a = ap.parse_args()
    g = pd.read_csv(a.games_csv, low_memory=False)
    cache = Path(a.cache)
    tc = pd.concat([team_conf(load_standings(s, cache)) for s in sorted(g.season.unique())], ignore_index=True)
    if a.show:
        print(tc.groupby("season").conf.nunique().rename("n_conferences").to_string())
        print(tc[tc.season == tc.season.max()].conf.value_counts().head(45).to_string()); return
    out = tag(g, tc, a.confs.split(","))
    untagged = out.home_conf.isna() | out.away_conf.isna()
    print(f"games: {len(out)}; missing a conference for at least one team: {untagged.mean():.1%}")
    cand = out[out.spread_home.notna() & (out.away_fg_gap_1h.abs() >= 10)] if "spread_home" in out else out
    summ = out.assign(lined=out.spread_home.notna() if "spread_home" in out else True).groupby("season").agg(
        games=("game_id", "size"), both_target=("both_target", "sum"),
        lined_both=("both_target", lambda s: (s & out.loc[s.index, "spread_home"].notna()).sum()) if "spread_home" in out else ("both_target", "sum"))
    print(summ.to_string())
    print(f"\nlined games, both teams in {a.confs}: {int((out.both_target & out.spread_home.notna()).sum())}"
          f"  | of those with a 1H FG% gap >= 10: {int((cand.both_target).sum())} games")
    out.to_csv(a.out, index=False); print("wrote", a.out)


if __name__ == "__main__":
    main()
