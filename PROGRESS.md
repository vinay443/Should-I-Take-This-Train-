# PROGRESS

Newest first. Say "continue from PROGRESS.md" to pick up where this stops.

## 2026-10-12

### Item 3: live readings in recommendations (done)
- `sitt.recommend`: the latest reading of a run taken today, at most
  `SITT_LIVE_MAX_AGE_MINUTES` (default 30) before the asked time, at the boarding station
  or upstream of it, replaces the predicted departure delay; the arrival keeps the predicted
  change en route. Options carry `live` (a `LiveReading`).
- A train whose scheduled time has passed but which is running late enough to still be
  caught is a candidate again. The train ahead's live delay feeds crowding.
- Marked in `/next` ("· live"), `/why`, `sitt-next` ("(live 10:15)") and a dashboard column.
- Behaviour change: when readings exist but none from earlier days of the trains asked
  about, the level is now `timetable`, not `baseline` (it used to claim a typical past
  delay it didn't have). One retrain test updated for this.
- Footer now warns about synthetic live readings at timetable level.
- 9 tests in `tests/test_live_recommend.py`. Checked on a copy of the real database:
  KYN -> CSMT at 10:20 on 6 Oct picks up the 10:06 fast running 17 min late (still to
  leave, passed over by the very-late rule) and five on-time live trains.
- Docs: `docs/recommender.md` "Live readings"; `.env.example`.

### Item 2: `sitt-next` command-line recommender (done)
- New `src/sitt/next_cli.py`, script `sitt-next`. Options `--at`, `--date`, `--train`,
  `--why`, `-n`, `--db`, `--no-model`. Reuses `sitt.bot.schedule.recommend_trains`, so it
  answers exactly what `/next` would. Never creates a database: a missing file is exit 2.
- `--train HH:MM` asks one minute before that train leaves, so it is always the first
  candidate even at peak, and prints a short block about that train.
- 15 tests in `tests/test_next_cli.py`. Run against the sample timetable and against a copy
  of the real database (KYN → CSMT at 08:00 on 13 Oct: baseline level, 15-car and ladies'
  special tags shown).
- README: "Ask from the command line".

### Item 1: hermetic network-guard test (done, PR #1)
- `test_a_real_connection_is_refused` failed on machines with `HTTPS_PROXY` set. Now
  clears proxy variables and uses `httpx.Client(trust_env=False)`.

### Environment check
- Local folder on your PC (`Documents\should_i_take_this_train`) is at the same commit as
  GitHub `main` (371b022 before this session), with only gitignored data/scratch files on
  top. Nothing uncommitted needed saving.
- Stack: Python 3.12 + uv, DuckDB, LightGBM, python-telegram-bot, Streamlit. `uv sync
  --locked`, `ruff check`, `ruff format --check` all clean; 597/598 tests passed before the
  fix above, 598/598 after.
- No GitHub workflow runs on a schedule (collect/probe/blocks are manual-only). No
  background collector was set up or run.
