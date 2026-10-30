# CBB halftime logger
Records, for every D-I men's game ESPN lists: the pregame line, the first-half box score at the break,
whatever odds ESPN shows at halftime (sampled every minute), and the final/2H result.

## Setup (once)
1. Create a **public** GitHub repo (public = unlimited free Actions minutes; private gets 2,000 min/month and this uses ~15 h/day).
2. Upload everything in this folder (keep the `.github/workflows/log.yml` path), commit to `main`.
3. Repo > Settings > Actions > General > Workflow permissions > "Read and write permissions".
4. Actions tab > "cbb-halftime-logger" > Run workflow (to start immediately; otherwise the cron starts it).
Data lands in `data/games/<date>/<event>.json`, committed every ~20 min.

## Reading it
`pip install pandas numpy; git pull; python report.py --gap 10`

## Day-1 check (most important)
Open any `data/games/*/*.json` after halftime and look at `odds_ticks`. If the halftime odds differ from `pregame`, ESPN
shows a live line and `cover_live` in report.py is usable (assumes a live FULL-GAME line; confirm). If they are identical, ESPN
only shows the pregame line at the break, and you'll need another source for halftime lines (the box scores are still logged).

## Notes
- Unofficial ESPN API; it can change without notice. Cron windows follow the real slate (weekday evenings; weekends ~11am-1am ET, EST). GitHub cron can start late; times shift 1h in March.
- Games with a final score but no halftime snapshot (logger down at the break) are skipped in the report.
