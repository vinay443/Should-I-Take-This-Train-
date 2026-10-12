# PROGRESS

Newest first. Say "continue from PROGRESS.md" to pick up where this stops.

## Where this stopped

Round 2 in progress (PLAN.md items 6-14). See the newest entry below for the last finished
item; the next unticked item in PLAN.md is what to do next.

## 2026-10-12

### Item 6: `sitt-health` in manual mode (done)
- `SITT_COLLECT_CADENCE_MINUTES` now defaults to 0 = collection by hand: no expected runs,
  no gaps, no "no successful run" alert; the report shows **Last run** (even before the
  window). Source-failure alerts still apply. Set 15 to get the old behaviour.
- Real-DB check: the report used to say "about 96 run(s) missed"; now "Runs: 0 recorded.
  Collection is by hand… Last run: 06 Oct 12:45 IST (6 d 6 h ago)".
- 5 new tests; the scheduled-mode tests now set a cadence explicitly.
- Also found by running `sitt-dq` on real data: 95335 timetable vs NTES mismatch (PLAN.md,
  open data question). Not changed.

### Item 4: docs for manual-only collection (done)
- README: live observations are from 5-6 Oct plus manual runs; "collect once, then ask"
  flow; `sitt-next` and live readings in Done; roadmap renumbered; layout lists
  `next_cli.py`; scripts marked optional.
- `docs/collector.md`: the Task Scheduler section is marked optional and not in use.
- `docs/retraining.md`: the weekly task is marked optional.
- Item 5 checked: no official 2027 Maharashtra holiday list yet.

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
