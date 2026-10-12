# PLAN

Goal: a user picks a route (and optionally a train and a time) and gets a delay and crowding
prediction with a short explanation, from a free, local-only stack. Collection is run by
hand only; nothing runs in the background.

Written 2026-10-12 after reviewing the README, docs/, the roadmap, commit history (Parts
0–10), the tests and the real local database (2,227 NTES readings at Kalyan, 5–6 Oct 2026).
There are no open GitHub issues and no TODO/FIXME markers in the code.

## Already done (before this plan)

- Real Central main-line timetable (895 trains) from the official PDFs, with supplements,
  overrides and movable holidays.
- Live collector for NTES (Mobond adapter present, off), run log, health report, DQ checks,
  Parquet export.
- Synthetic data generator, baselines, LightGBM delay model, real-data retraining pipeline
  with promotion gates.
- Rule-based crowding estimate adjusted by the user's own crowd reports.
- Recommender (`/next`, `/why`) in the Telegram bot and a Streamlit dashboard.
- CI: ruff + pytest, network blocked in tests.

## Remaining

### 1. Make the network-guard test hermetic — [x]
`test_a_real_connection_is_refused` fails on any machine with an HTTP(S) proxy set, because
`httpx.Client()` sends the request to the proxy instead of hitting the guard.
**Done when:** the test passes with and without `HTTPS_PROXY` set; full suite green.

### 2. `sitt-next` command-line recommender — [x]
The MVP today needs Telegram or Streamlit. Add `uv run sitt-next KYN CSMT` with
`--at HH:MM`, `--date YYYY-MM-DD`, `--train HH:MM` (focus on one train), `--why` (full
explanation), `-n` (how many trains), `--db`.
**Done when:** it prints the one-line recommendation and a table of the next trains
(departure, predicted arrival, crowding, tags, prediction source); unknown stations give a
clear error and exit code 2; `--train` with no matching train says so; tests cover output,
`--at`/`--date`, `--train` and errors; verified by running it against the sample timetable
and against the real database.

### 3. Use live readings in recommendations (roadmap item 6) — [x]
Today only *past days'* readings feed predictions (and cancellations). A reading taken a few
minutes ago that says "running 9 min late" is ignored.
Use the latest same-day reading of each candidate at the boarding station (or the
destination), if it is recent (`SITT_LIVE_MAX_AGE_MINUTES`, default 30), to override the
predicted departure delay; carry it to the arrival when there is no better arrival
prediction. Mark the option as live, use the live delay of the train ahead for crowding,
and say so in `/next`, `/why`, `sitt-next` and the dashboard.
**Done when:** unit tests cover: a fresh reading overrides the baseline/timetable; a stale or
other-day reading is ignored; the train-ahead delay reaches crowding; the reason/explanation
mentions the live reading; works at timetable level (no history). Verified on the real DB
with a reading time inside the collected window.

### 4. Docs reflect how the project is run now — [ ]
The README says data is collected every 15 minutes; collection is now manual only (the
Task Scheduler collector was switched off). Document `sitt-next`, the live-reading rule
(`docs/recommender.md`), the manual "collect once, then ask" flow, and the `.env.example`
setting.
**Done when:** README install/run/test sections are accurate, no doc tells the reader that a
background collector is running, `.env.example` lists the new setting.

### 5. 2027 movable holidays — [ ] (blocked on outside data)
Maharashtra usually publishes the next year's list in November–December. The 2027 entries
are already in `holidays.toml`, marked tentative.
**Done when:** the official 2027 list is out and the entries are updated to
`reported`/`confirmed`. Nothing to do before then.

## Out of scope / needs you

- Asking Mobond for permission (the adapter stays off until then).
- Logging crowd reports and letting real readings accumulate (manual runs only).
- Refreshing the timetable when Central Railway publishes a new edition.
