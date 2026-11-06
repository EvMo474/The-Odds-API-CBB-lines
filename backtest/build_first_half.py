#!/usr/bin/env python3
"""
Build a first-half / second-half shooting dataset for men's college basketball
from the hoopR / sportsdataverse ESPN play-by-play (no API key needed).

One row per game with:
  * halftime score and regulation (end of 2nd half) score, overtime flag
  * each team's 1H and 2H FGM/FGA/FG%, 3PM/3PA, FTM
  * each team's season-to-date 1H FG% going into the game (prior games only)
  * away_fg_gap_1h = away FG% minus home FG% in the first half (percentage points)
  * neutral_site flag from the schedule file (ESPN's home/away is arbitrary there)
  * data-quality flags (pbp_complete_*, pts_match_*)

Setup:
    pip install pandas pyarrow requests

Usage:
    python build_first_half.py --inspect 2024
    python build_first_half.py --seasons 2012-2026 --out cbb_first_half.csv

Season = ending year (2026 means 2025-26). Downloads are cached in --cache.
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

PBP_URL = ("https://github.com/sportsdataverse/hoopR-mbb-data/raw/main/"
           "mbb/pbp/parquet/play_by_play_{season}.parquet")
SCHED_URL = ("https://github.com/sportsdataverse/hoopR-mbb-data/raw/main/"
             "mbb/schedules/parquet/mbb_schedule_{season}.parquet")

# Column name candidates (first match wins) so small schema changes don't break us.
CANDIDATES = {
    "game_id": ["game_id"],
    "seq": ["sequence_number", "game_play_number", "id"],
    "period": ["period_number", "period"],
    "scoring": ["scoring_play"],
    "shooting": ["shooting_play"],
    "score_value": ["score_value"],
    "team_id": ["team_id"],
    "type_text": ["type_text"],
    "text": ["text"],
    "home_score": ["home_score"],
    "away_score": ["away_score"],
    "home_id": ["home_team_id"],
    "away_id": ["away_team_id"],
    "home_name": ["home_team_name", "home_team_location"],
    "away_name": ["away_team_name", "away_team_location"],
    "date": ["game_date", "game_date_time"],
}
REQUIRED = ["game_id", "period", "scoring", "shooting", "team_id",
            "home_score", "away_score", "home_id", "away_id"]


def resolve_columns(cols):
    found = {}
    for key, names in CANDIDATES.items():
        for n in names:
            if n in cols:
                found[key] = n
                break
    missing = [k for k in REQUIRED if k not in found]
    if missing:
        raise SystemExit(f"Missing required columns {missing}. "
                         f"Run --inspect to see what this file has.")
    return found


def download(url, path):
    import requests
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return path
    print(f"downloading {url}", file=sys.stderr)
    with requests.get(url, stream=True, timeout=120,
                      headers={"User-Agent": "cbb-first-half/1.0"}) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return path


def pbp_path(season, cache):
    return download(PBP_URL.format(season=season), cache / f"play_by_play_{season}.parquet")


def load_pbp(path):
    import pyarrow.parquet as pq
    names = pq.ParquetFile(path).schema.names
    cmap = resolve_columns(names)
    use = sorted(set(cmap.values()))
    df = pq.read_table(path, columns=use).to_pandas()
    return df.rename(columns={v: k for k, v in cmap.items()})


def _flags(df):
    text = df["text"].fillna("").str.lower() if "text" in df else pd.Series("", index=df.index)
    ttype = df["type_text"].fillna("").str.lower() if "type_text" in df else pd.Series("", index=df.index)
    is_ft = ttype.str.contains("freethrow") | text.str.contains("free throw")
    shooting = df["shooting"].fillna(False).astype(bool)
    scoring = df["scoring"].fillna(False).astype(bool)
    df["is_fga"] = shooting & ~is_ft
    df["is_fgm"] = df["is_fga"] & scoring
    is_three = text.str.contains("three point")
    if "score_value" in df:
        is_three = is_three | (df["score_value"] == 3)
    df["is_3pa"] = df["is_fga"] & is_three
    df["is_3pm"] = df["is_3pa"] & scoring
    df["is_ftm"] = shooting & is_ft & scoring
    return df


def _team_half(pbp, period):
    h = _flags(pbp[pbp["period"] == period].copy())
    keep = h[h["is_fga"] | h["is_ftm"]]
    return (keep.groupby(["game_id", "team_id"])
            [["is_fgm", "is_fga", "is_3pm", "is_3pa", "is_ftm"]]
            .sum().astype(int)
            .rename(columns={"is_fgm": "fgm", "is_fga": "fga", "is_3pm": "3pm",
                             "is_3pa": "3pa", "is_ftm": "ftm"})
            .reset_index())


def build_game_table(pbp, season=None):
    """pbp: DataFrame with the normalized column names from CANDIDATES keys."""
    p12 = pbp[pbp["period"].isin([1, 2])]
    if p12.empty:
        return pd.DataFrame()

    # Regulation score = max running score through period 2 (OT excluded).
    games = (p12.groupby("game_id")
             .agg(home_id=("home_id", "first"), away_id=("away_id", "first"),
                  reg_home=("home_score", "max"), reg_away=("away_score", "max"))
             .reset_index())
    ht = (pbp[pbp["period"] == 1].groupby("game_id")
          .agg(ht_home=("home_score", "max"), ht_away=("away_score", "max")).reset_index())
    games = games.merge(ht, on="game_id", how="inner")
    ot = (pbp["period"] > 2).groupby(pbp["game_id"]).any().rename("went_ot").reset_index()
    games = games.merge(ot, on="game_id", how="left")
    for key in ("home_name", "away_name", "date"):
        if key in p12:
            games[key] = p12.groupby("game_id")[key].first().reindex(games["game_id"]).values
    if season is not None:
        games["season"] = season

    out = games
    for period, suf in ((1, ""), (2, "_2h")):
        team = _team_half(pbp, period)
        for side in ("home", "away"):
            ren = {"team_id": f"{side}_id"}
            for c in ("fgm", "fga", "3pm", "3pa", "ftm"):
                ren[c] = f"{side}_{c}{suf}"
            out = out.merge(team.rename(columns=ren), on=["game_id", f"{side}_id"], how="left")
    num = [f"{side}_{c}{suf}" for side in ("home", "away") for suf in ("", "_2h")
           for c in ("fgm", "fga", "3pm", "3pa", "ftm")]
    out[num] = out[num].fillna(0).astype(int)

    for side in ("home", "away"):
        for suf in ("", "_2h"):
            fga = out[f"{side}_fga{suf}"]
            out[f"{side}_fg_pct_1h" if suf == "" else f"{side}_fg_pct_2h"] = (
                out[f"{side}_fgm{suf}"] / fga.where(fga > 0) * 100).round(2)

    # Positive = away team shot better than home in the first half.
    # Your filter: road team shot 10+ percentage points worse -> away_fg_gap_1h <= -10.
    out["away_fg_gap_1h"] = (out["away_fg_pct_1h"] - out["home_fg_pct_1h"]).round(2)
    out["away_fg_gap_2h"] = (out["away_fg_pct_2h"] - out["home_fg_pct_2h"]).round(2)

    # Second-half points in regulation and margins (away minus home).
    out["h2_home_pts"] = out["reg_home"] - out["ht_home"]
    out["h2_away_pts"] = out["reg_away"] - out["ht_away"]
    out["ht_margin_away"] = out["ht_away"] - out["ht_home"]
    out["h2_margin_away"] = out["h2_away_pts"] - out["h2_home_pts"]
    out["reg_margin_away"] = out["reg_away"] - out["reg_home"]

    # Data-quality flags.
    out["pbp_complete_1h"] = (out["home_fga"] + out["away_fga"]) >= 40
    out["pbp_complete_2h"] = (out["home_fga_2h"] + out["away_fga_2h"]) >= 40
    out["pts_match_1h"] = ((2 * out["home_fgm"] + out["home_3pm"] + out["home_ftm"] == out["ht_home"]) &
                           (2 * out["away_fgm"] + out["away_3pm"] + out["away_ftm"] == out["ht_away"]))
    out["pts_match_2h"] = ((2 * out["home_fgm_2h"] + out["home_3pm_2h"] + out["home_ftm_2h"] == out["h2_home_pts"]) &
                           (2 * out["away_fgm_2h"] + out["away_3pm_2h"] + out["away_ftm_2h"] == out["h2_away_pts"]))
    return out


def add_prior_norms(games, min_games=5):
    """Season-to-date 1H FG% for each team BEFORE each game (no lookahead)."""
    sort_key = "date" if "date" in games and games["date"].notna().any() else "game_id"
    rows = []
    for side in ("home", "away"):
        rows.append(games[["season", "game_id", sort_key, f"{side}_id",
                           f"{side}_fgm", f"{side}_fga"]]
                    .rename(columns={f"{side}_id": "team_id", f"{side}_fgm": "fgm",
                                     f"{side}_fga": "fga"})
                    .assign(side=side))
    long = pd.concat(rows, ignore_index=True)
    long = long[long["fga"] > 0].sort_values(["season", "team_id", sort_key, "game_id"])
    g = long.groupby(["season", "team_id"])
    long["prior_fgm"] = g["fgm"].cumsum() - long["fgm"]
    long["prior_fga"] = g["fga"].cumsum() - long["fga"]
    long["prior_games"] = g.cumcount()
    ok = long["prior_games"] >= min_games
    long["prior_fg_pct_1h"] = (long["prior_fgm"] / long["prior_fga"] * 100).where(ok).round(2)
    for side in ("home", "away"):
        part = (long[long["side"] == side][["game_id", "prior_fg_pct_1h", "prior_games"]]
                .rename(columns={"prior_fg_pct_1h": f"{side}_prior_fg_pct_1h",
                                 "prior_games": f"{side}_prior_games"}))
        games = games.merge(part, on="game_id", how="left")
        games[f"{side}_fg_pct_vs_prior"] = (games[f"{side}_fg_pct_1h"]
                                            - games[f"{side}_prior_fg_pct_1h"]).round(2)
    return games


def add_schedule_info(games, season, cache):
    """Merge neutral_site (and season type if present) from the schedule file.
    Never fails the build: on any problem the columns are just left empty."""
    try:
        import pyarrow.parquet as pq
        path = download(SCHED_URL.format(season=season), cache / f"mbb_schedule_{season}.parquet")
        names = pq.ParquetFile(path).schema.names
        idc = next((c for c in ("game_id", "id") if c in names), None)
        extra = [c for c in ("neutral_site", "season_type", "type_abbreviation",
                             "conference_competition", "tournament_id") if c in names]
        if idc is None or not extra:
            print(f"season {season}: schedule has no usable columns {names[:15]}...",
                  file=sys.stderr)
            return games
        s = pq.read_table(path, columns=[idc] + extra).to_pandas().rename(columns={idc: "game_id"})
        s["game_id"] = pd.to_numeric(s["game_id"], errors="coerce")
        s = s.dropna(subset=["game_id"]).drop_duplicates("game_id")
        s["game_id"] = s["game_id"].astype("int64")
        games = games.copy()
        games["game_id"] = pd.to_numeric(games["game_id"], errors="coerce").astype("int64")
        return games.merge(s, on="game_id", how="left")
    except Exception as e:
        print(f"season {season}: schedule merge skipped ({e})", file=sys.stderr)
        return games


def parse_seasons(s):
    if "-" in s:
        a, b = s.split("-")
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in s.split(",")]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seasons", help="e.g. 2024, 2018-2026, or 2022,2023")
    ap.add_argument("--inspect", type=int, metavar="SEASON",
                    help="print columns and a sanity check for one season")
    ap.add_argument("--cache", default="pbp_cache")
    ap.add_argument("--out", default="cbb_first_half.csv")
    ap.add_argument("--min-games", type=int, default=5,
                    help="games a team needs before its season norm is used")
    a = ap.parse_args()
    cache = Path(a.cache)

    if a.inspect:
        import pyarrow.parquet as pq
        p = pbp_path(a.inspect, cache)
        print("pbp columns:", pq.ParquetFile(p).schema.names)
        g = build_game_table(load_pbp(p), a.inspect)
        g = add_schedule_info(g, a.inspect, cache)
        print(g.head(5).T.to_string())
        print(f"\ngames: {len(g)}  complete 1H pbp: {int(g['pbp_complete_1h'].sum())}")
        print("1H points reconcile:", round(g["pts_match_1h"].mean() * 100, 1), "%   "
              "2H points reconcile:", round(g["pts_match_2h"].mean() * 100, 1), "%  (want ~95%+)")
        print("avg 1H FG% home/away:", round(g["home_fg_pct_1h"].mean(), 1),
              round(g["away_fg_pct_1h"].mean(), 1), " <- expect low-40s")
        if "neutral_site" in g:
            print("neutral-site games:", int(g["neutral_site"].fillna(False).astype(bool).sum()))
        return

    if not a.seasons:
        ap.error("give --seasons or --inspect")
    frames = []
    for s in parse_seasons(a.seasons):
        try:
            g = build_game_table(load_pbp(pbp_path(s, cache)), s)
            frames.append(add_schedule_info(g, s, cache))
        except Exception as e:  # keep going if one season fails
            print(f"season {s} failed: {e}", file=sys.stderr)
    games = pd.concat(frames, ignore_index=True)
    games = add_prior_norms(games, a.min_games)
    games.to_csv(a.out, index=False)
    print(f"wrote {len(games)} games to {a.out} "
          f"({int(games['pbp_complete_1h'].sum())} with complete 1H pbp, "
          f"{int(games['pts_match_1h'].sum())} reconcile to halftime score)")


if __name__ == "__main__":
    main()
