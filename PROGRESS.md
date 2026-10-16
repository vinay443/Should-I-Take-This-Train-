# PROGRESS

Newest first. Say "continue from PROGRESS.md" to pick up where this stops.

## Where this stopped (2026-10-16)

**Next: PLAN.md round 7 (items 30-31).** Round 6 is done except item 26 (Harbour line),
which is blocked on the Harbour PDFs (see PLAN.md).

Real database: not copied (folder access declined on 2026-10-13). Work uses the sample
timetable unless the user connects the `data` folder.

## 2026-10-16

### Item 29: saved answers drop trains that have left (done, PR #31)
- Offline, a saved answer now leaves out trains whose predicted departure is more than a
  minute past, with a note ("1 train in the saved answer has left since…"). If the picked
  train has left, the page says so, lists the saved trains still to come on the board with
  no pick, and hides "Why this train?"; if every train has left it says that.
- `scripts/check_web_offline.py` moves the browser's clock past the first train and then
  three days on, and checks both cases at phone and desktop width, light and dark.

### Item 28: runs grouped the same in any time zone (done, PR #30)
- A run's service day came from `date_trunc('day', …)` on a TIMESTAMPTZ, which follows
  DuckDB's session time zone. On the PC (IST) the day starts at 03:00 Mumbai time as
  intended; in CI (UTC) it started at **08:30** Mumbai time, so a train due to arrive 08:29
  and leave 08:31 became two runs there. New `sitt.tz.mumbai_sql()` uses the fixed +05:30
  offset; used by live_drift, line_state, synth compare, patterns and the dashboard's
  cancelled-days count.
- Numbers in docs are unchanged: they were computed on the PC, where the session was IST.
- New test runs the same data under UTC, IST and Los Angeles; it fails on the old code.

### Item 27: independent review of round 6 (done, PR #29)
A separate agent reviewed PRs #26-#28 cold, checking the service worker in a real Chromium.
Five findings, all fixed:
1. HIGH: the service worker stored *any* page opened in the browser (the API docs, raw JSON
   from `/api/...`) as the main page, so offline the home-screen icon could open Swagger or
   JSON. Now only the app's two pages are stored; others fall back to the main page offline.
   `scripts/check_web_offline.py` now opens the API docs and JSON before going offline and
   checks `/` is still the app (fails on the old worker, passes on the new one).
2. MEDIUM: the first load after an update could run the new page with the old cached script.
   Pages now load styles and scripts as `?v=<hash>` and the worker matches the full URL.
3. LOW: the hourly chart had no bars for 02:00-03:59; it now shows all 24 hours.
4. LOW: two tests only grepped source; replaced by checks of what is served, plus the
   browser check above.
5. LOW: docs said the page installs full screen from `http://<pc-ip>`; Chrome only treats
   that as a shortcut. Docs corrected.

### Item 26: Harbour line — blocked
The cloud workspace can't reach Central Railway's site (proxy 403) and the PC copies need
folder access. Recorded in PLAN.md with what's needed.

### Item 25: delay patterns in the web app (done, PR #28)
- `/patterns` page and `GET /api/patterns` (`src/sitt/patterns.py`): median delay, 90th
  percentile and share 5+ min late, by part of the day and by hour, per direction, plus fast
  vs slow. Each run counts once with its last reading taken close to the train's time
  (the live-drift rule, now shared as `live_drift.runs_ctes`); real suburban readings only;
  cells under 5 trains drawn pale and marked; a caveat says how much it rests on.
- Charts are plain SVG drawn at the width shown (labels stay readable at 390 px), with
  hover/focus tooltips. Checked in headless Chromium at 390 and 1280 px, light and dark, on
  an invented demo database (569 runs) and on one with no readings (empty state); no script
  errors, no sideways scrolling. 6 new tests.

### Item 24: web app on a phone (done, PR #27)
- `manifest.webmanifest` + icons (`scripts/make_web_icons.py`, 192/512, maskable), `/sw.js`
  (cache version = hash of the static files; keeps only the page shell, station list and
  status line; never recommendations or writes), page meta for Android and iOS.
- Offline fallback in the page: the last 8 answers are kept in the browser; when the app
  can't be reached the saved answer is shown with a **Not live** note and the time it was
  saved; trips with no saved answer and crowd logs say clearly what happened.
- Honest limit, in `docs/web.md`: service workers need https or localhost, so on a phone
  over `http://<pc-ip>` the page itself can't open with the PC off; the saved-answer
  fallback works while the page is open.
- Checked in headless Chromium with `scripts/check_web_offline.py` (new): Chrome reports no
  installability errors, the worker controls the page, and after stopping the server the
  reload shows the saved answer; phone and desktop width, light and dark, no script errors.
  7 new tests (`tests/web/test_installable.py`).

## 2026-10-13 (round 6)

### Item 23: CI on Windows (done, PR #26)
- New `windows` job in `.github/workflows/ci.yml` (`windows-latest`, `uv sync --locked`,
  `uv run pytest`), without `PYTHONUTF8`, so encoding bugs a Windows user would hit still
  show up. Green on the first run: nothing Windows-only needed fixing (all text I/O already
  passes an explicit encoding). Lint/format stay on the Linux job only.

## 2026-10-13

### Item 22: web app (done, PR #23)
- `src/sitt/web/`: FastAPI JSON API (`/api/status`, `/api/stations`, `/api/recommend`,
  `POST /api/collect`, `GET/POST /api/reports`) and a static page (HTML/CSS/JS, no build
  step): indicator-board hero, next trains with arrival, delay and a crowding meter, "Why
  this train?", Check live first (on demand only), crowd logging matched to trains, saved
  routes. Fonts bundled (OFL), works offline. `uv run sitt-web` (`--host 0.0.0.0` for a phone
  on the same Wi-Fi). Docs: `docs/web.md`.
- Checked in Chromium at 390 px and 1280 px, light and dark, on a copy of the real DB, and
  every interaction clicked through. 22 API tests.
- Independent review found 8 defects (3 high: mixed DuckDB connections failing under load,
  a false docs claim about sharing the DB with the bot, crowd reports logged against the
  wrong station after a swap), all fixed and re-verified by the reviewer; its re-check found
  one regression ("Another time" ignored the "earlier hour means tomorrow" rule), fixed.

## 2026-10-12

### Correction: on-time live readings no longer override (live_drift re-measured)
- Fixing the line-state look-ahead showed `live_drift` had the same flaw: lead was measured
  from the train's *expected* time (which includes its delay). Now measured from the
  scheduled time, with an "after its time" row; arrival/departure counted as one run.
- Corrected finding: 33-39% of suburban trains read "on time" before their scheduled time
  ended 5+ min late, at **every** lead (16% in the last 15 min was an artefact).
- So the recommender's 15-minute trust window for on-time readings (PR #9) is gone: on-time
  or early readings never override; delayed readings still count as a floor.
  `docs/live-drift.md` and `docs/recommender.md` rewritten. Tests updated, 1 new.

### Second independent review (rounds 3-4): fixed
A second cold review found 4 issues, 3 confirmed; all fixed with tests:
1. `line_state` keyed runs by event, so a run with arrival and departure readings counted
   twice (on the real Kalyan data NTES gives one or the other, so the count stays 134).
2. HIGH: `line_state` asked "N min before" the train's *expected* time, which includes its
   delay: look-ahead. Now relative to the scheduled time. Corrected numbers: a train's own
   reading barely beats the timetable (6.3 vs 6.7 min at 15 min ahead); the line state is
   better 30-60 min ahead (5.9-6.0). Still inconsistent by session; still not used.
   `docs/line-state.md` rewritten with the corrected tables.
3. `line_state` mixed stations within a run; now one run per station.
4. `/next … live` and `sitt-next --collect` wrote to `./data` relative to wherever they were
   started; now beside the database by default.

### Item 21: `/next … live` (done)
- `/next KYN CSMT live` runs one collection round (in a thread) and then answers, starting
  with "Live check just now: …". The helper moved to `sitt.ingest.live.local.collect_now`,
  shared with `sitt-next --collect`. Without `live` nothing is fetched (tested). 2 new tests.

### Items 19-20: timetable currency; flags in the baseline (done)
- The 15 Aug 2026 15-car changes are in the timetable; the 29 June 2026 AC conversions (12
  services) are not. README says so. **Needs you:** the newer AC supplement PDF from Central
  Railway (couldn't open their page without your approval).
- The baseline already leaves out `dq_flags` readings; test added, data-quality doc fixed.

### Item 18: bot handler tests (done)
- `tests/bot/test_log_flow.py`: the full `/log` button flow to a saved report and `/mylogs`,
  double taps and stale buttons, cancel by button and by `/cancel`, typed time/station
  replies, conflicting one-line logs, unrecognised words. `bot/handlers.py` coverage within
  the bot tests: 72% -> 87%. 7 new tests.
- Fixed: `.coverage` had been committed by mistake in PR #12; removed and gitignored.

### Item 17: two-minute quick start (done)
- README "Try it in two minutes": four commands from a fresh clone to a `sitt-next` answer
  on the sample timetable. Verified in a fresh clone of `main`.

### Item 16: line-state nowcast backtest (done; not used in recommendations)
- New `python -m sitt.models.line_state [--by-direction]`: predicts each run's final delay
  15-60 min ahead from zero / own reading / other same-direction trains' honest readings /
  max(own, line). Write-up `docs/line-state.md`.
- Result: max(own, line) has the lowest MAE in every row (5.3 vs 5.7 min at 15 min ahead),
  all from up trains, but not in both sessions and no better within 5 min. Below the bar
  set in PLAN.md, so the recommender is unchanged. 2 new tests.

### Item 15: 95335 mismatch resolved (no override)
- Checked the 2026 15-car supplement PDF (on your PC in `scratch/`): 95335 is Dadar 23:23 ->
  Kalyan 00:18, exactly as loaded. NTES's 21:54 is another schedule under the same number.
  Kept flagged; written up in `docs/data-quality.md` ("Cases looked into").

### Item 14: independent review (done)
A separate Claude Code agent reviewed all of this session's changes cold and reported 5
defects (3 confirmed by running code). All fixed, each with a regression test:
1. HIGH: asked mid-minute (the bot always is), a train with a late live reading was listed
   twice, and without readings the first train could be its own "train ahead" for crowding.
2. MEDIUM: an "early" live reading of any age was trusted and moved departures earlier. Now
   early readings follow the on-time trust rule, and a departure is never before its time.
3. LOW: `sitt-next --at/--train` with a past time and no `--date` now means tomorrow, as
   in the bot.
4. LOW: `--train` prefers the asked day and names the day when it's another.
5. LOW: `/why` no longer says "no past observations" when today's readings exist.
Plus: the dashboard says "collection is by hand" only when no cadence is set.

### Item 13: test coverage (done)
- `pytest-cov` added as a dev dependency (free). Coverage: 92% of `src/sitt` (6,916
  statements). Weakest: `dashboard/app.py` (0%, a measurement artefact: AppTest runs it as
  a script), `ingest/live/probe.py` 51% (manual network probe), `ingest/live/load.py` 72%
  (CLI glue), `bot/handlers.py` 80% (Telegram callbacks).
- New tests for `sitt-next` live/cancelled/history/synthetic rows and a crashed
  `--collect`, and for the loader CLI. 671 tests.

### Item 12: dashboard freshness (done)
- Sidebar, with real observations: "Last collected 06 Oct 12:45 (6 days ago). Collection is
  by hand: `uv run sitt-next KYN CSMT --collect` fetches fresh readings." (newest real
  reading or collector run). Live readings were already shown in the recommender's Live
  column and notes (item 3). 2 new tests, incl. a headless app run on a real-style DB.

### Item 11: synthetic vs real (done, doc + tool; generator unchanged)
- New `python -m sitt.synth.compare`: real vs synthetic delays by direction and fast/slow,
  restricted to the stations, kinds of day and hours the real data covers.
- Finding: at Kalyan, real up trains (towards CSMT) were ~11-12 min late (median), down
  trains ~93% on time; the synthetic data has it the other way round (delay built from
  each train's origin). Held in both sessions. Write-up: `docs/synthetic-vs-real.md`.
- Deliberately not retuned: two sessions only, and NTES shows only trains from beyond
  Kalyan. The doc lists what to change if more data agrees. 3 new tests.

### Item 10: how live delays drift (done)
- New `python -m sitt.models.live_drift`: pairs each reading with the run's last reading
  taken within 15 min of the train's time, and summarises drift by lead and by whether the
  reading said "on time". Results and caveats in `docs/live-drift.md`.
- Finding (suburban, real KYN data): NTES says "on time" until a train is close; a third of
  on-time readings 15-60 min ahead ended 5+ min late; no train ended 3+ min earlier than read.
- Recommender: an on-time live reading counts only within 15 min of the train
  (`LIVE_ON_TIME_TRUST_MINUTES`); delayed readings always count, explained as a floor.
  Real DB, KYN 10:20 on 6 Oct: 3 far-ahead "on time" readings no longer override.
- No drift added to predictions yet: only 38 "late" readings. Re-run when there's more data.
- 5 new tests.

### Item 9: `sitt-next --collect` (done)
- One `sitt-collect` round (NTES; Mobond only with both switches) right before answering;
  prints "Live check just now: ntes N readings." A failed fetch still gives an answer.
  `--data-dir` for where batches go. No network without the flag (tested).
- Not tried against the real NTES from here (no route from the cloud workspace); tests use
  the recorded NTES board fixture. Worth one real run on your PC.

### Item 8: "Did you mean…?" for stations (done)
- `StationDirectory.suggest` (stdlib difflib over aliases, codes and names); the bot's
  `/next` and `sitt-next` both add "Did you mean Thane (TNA)?" to the unknown-station error.
  Real DB: `sitt-next Kalyn Dadr` suggests Kalyan, Kelavli or Kalva. 13 new tests.

### Item 7: `/next` with a time (done)
- `sitt.bot.schedule.split_when`: trailing `8:00`, `8 pm`, `8.30am`, `today`, `tomorrow`, in
  either order. A time more than an hour gone today means tomorrow. Bare numbers are not
  times. The reply header then says "…, at 08:00 on Tue 13 Oct".
- Help text, usage message, bot docs and README updated. 14 new tests.

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
