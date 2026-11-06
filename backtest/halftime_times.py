#!/usr/bin/env python3
"""
Free prep for the Odds API halftime-line pull: when does each game tip, and when is halftime in real-world time?

  python backtest/halftime_times.py --seasons 2022-2026 --out backtest/out

Per game it writes (UTC):
  tip_sched_utc   scheduled tip from the hoopR schedule file (game_date_time)
  first_play_utc  first timestamped play, end1h_utc = last 1H play, start2h_utc = first 2H play  (play-by-play wall clock)
  ht_start_utc    halftime start  = end1h_utc when the timestamps pass sanity checks, else tip + the season's median
                  (tip -> halftime) learned from the good games   [ht_src = "pbp" | "estimate" | ""]
  snap_utc        ht_start + 5 minutes: the moment the Odds API historical snapshot should be requested
                  (same "5 minutes into the break" rule the live logger uses)
Sanity checks per game: break between halves 8-35 min, first play to end of 1H 40-110 min.
Writes game_times.csv.gz and times_report.txt (coverage, medians, how many games had to be estimated).
"""
import argparse, sys
from pathlib import Path
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_first_half as bf


def season_times(season, cache):
    import pyarrow.parquet as pq
    ppath = bf.pbp_path(season, cache)
    names = pq.ParquetFile(ppath).schema.names
    per = next((c for c in ("period_number", "period") if c in names), None)
    wall = [c for c in names if "wall" in c.lower()]
    if per is None or not wall or "game_id" not in names:
        print(f"season {season}: no period/wall-clock columns (found {wall}); using estimates only", file=sys.stderr)
        g = pd.DataFrame({"game_id": pd.Series(dtype="int64")})
    else:
        df = pq.read_table(ppath, columns=["game_id", per, wall[0]]).to_pandas()
        df["t"] = pd.to_datetime(df[wall[0]], errors="coerce", utc=True)
        df = df.dropna(subset=["t"])
        g = df.groupby("game_id").agg(first_play_utc=("t", "min")).reset_index()
        g = g.merge(df[df[per] == 1].groupby("game_id").t.max().rename("end1h_utc").reset_index(), on="game_id", how="left")
        g = g.merge(df[df[per] == 2].groupby("game_id").t.min().rename("start2h_utc").reset_index(), on="game_id", how="left")
        g["game_id"] = pd.to_numeric(g.game_id, errors="coerce")
        g = g.dropna(subset=["game_id"]); g["game_id"] = g.game_id.astype("int64")
    try:
        spath = bf.download(bf.SCHED_URL.format(season=season), cache / f"mbb_schedule_{season}.parquet")
        sn = pq.ParquetFile(spath).schema.names
        idc = next((c for c in ("game_id", "id") if c in sn), None)
        tc = next((c for c in ("game_date_time", "start_date", "date") if c in sn), None)
        s = pq.read_table(spath, columns=[idc, tc]).to_pandas().rename(columns={idc: "game_id", tc: "tip_sched_utc"})
        s["game_id"] = pd.to_numeric(s.game_id, errors="coerce"); s = s.dropna(subset=["game_id"]).drop_duplicates("game_id")
        s["game_id"] = s.game_id.astype("int64"); s["tip_sched_utc"] = pd.to_datetime(s.tip_sched_utc, errors="coerce", utc=True)
    except Exception as e:
        print(f"season {season}: schedule times unavailable ({e})", file=sys.stderr)
        s = pd.DataFrame({"game_id": pd.Series(dtype="int64"), "tip_sched_utc": pd.Series(dtype="datetime64[ns, UTC]")})
    out = s.merge(g, on="game_id", how="outer"); out["season"] = season
    return out


def finish(t):
    t = t.copy()
    brk = (t.start2h_utc - t.end1h_utc).dt.total_seconds() / 60
    h1 = (t.end1h_utc - t.first_play_utc).dt.total_seconds() / 60
    good = brk.between(8, 35) & h1.between(40, 110)
    t["break_min"] = brk.round(1).where(good); t["h1_len_min"] = h1.round(1).where(good)
    anchor = t.tip_sched_utc.fillna(t.first_play_utc)
    lag = ((t.end1h_utc - anchor).dt.total_seconds() / 60).where(good)
    med = lag.groupby(t.season).transform("median").fillna(lag.median())
    est = anchor + pd.to_timedelta(med, unit="m")
    t["ht_start_utc"] = t.end1h_utc.where(good).fillna(est)
    t["ht_src"] = np.where(good, "pbp", np.where(t.ht_start_utc.notna(), "estimate", ""))
    t["snap_utc"] = t.ht_start_utc + pd.Timedelta(minutes=5)
    return t, lag


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2022-2026"); ap.add_argument("--out", default="backtest/out")
    ap.add_argument("--cache", default="backtest_work/pbp_cache")
    a = ap.parse_args()
    cache = Path(a.cache); out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    t = pd.concat([season_times(s, cache) for s in bf.parse_seasons(a.seasons)], ignore_index=True)
    t, lag = finish(t)
    cols = ["game_id", "season", "tip_sched_utc", "first_play_utc", "end1h_utc", "start2h_utc", "break_min", "h1_len_min", "ht_start_utc", "ht_src", "snap_utc"]
    t[cols].to_csv(out / "game_times.csv.gz", index=False, compression="gzip")
    L = [f"games {len(t)}", "", "halftime time source by season (pbp = real timestamps passed checks):",
         t.groupby(["season", "ht_src"]).size().unstack(fill_value=0).to_string(), "",
         "median tip -> halftime start, minutes (good games only):", lag.groupby(t.season).median().round(1).to_string(), "",
         "median break length, minutes:", t.groupby("season").break_min.median().round(1).to_string(), "",
         "median first-half length (first play to last 1H play), minutes:", t.groupby("season").h1_len_min.median().round(1).to_string(), "",
         f"games with a snapshot time: {int(t.snap_utc.notna().sum())}"]
    (out / "times_report.txt").write_text("\n".join(L)); print("\n".join(L))


if __name__ == "__main__":
    main()
