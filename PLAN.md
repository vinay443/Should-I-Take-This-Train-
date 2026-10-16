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

### 4. Docs reflect how the project is run now — [x]
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
Checked 2026-10-12: no official 2027 notification found yet, only third-party calendars.

## Round 2 (added 2026-10-12, self-directed)

Picked by value after running `sitt-dq`, `sitt-health` and `sitt-retrain` on a copy of the
real database. Each item ends with a PR merged on green CI and a PROGRESS.md entry.

### 6. `sitt-health` understands manual collection — [x]
With no schedule, the health report says "about 96 runs missed" every day, which reads as
broken. Make the expected cadence optional: `SITT_COLLECT_CADENCE_MINUTES=0` (the new
default, matching how the project is run) means manual; the report then shows the last
runs, their sources and data freshness, with no gap accounting and no "no run" alert.
**Done when:** tests cover manual and scheduled modes; real-DB run reads sensibly; docs say
how to switch back to a cadence.

### 7. `/next` in the bot accepts a time — [x]
`/next KYN CSMT 8:00` (and `tomorrow 8:00`) like `sitt-next --at`. **Done when:** parsing
tests and a handler test pass; help text and bot docs updated.

### 8. "Did you mean…?" for unknown stations — [x]
`sitt-next KYN CSTM` and `/next KYN Thaane` suggest the closest station names/codes
(stdlib `difflib`). **Done when:** tests for typos, codes and names; both entry points use it.

### 9. `sitt-next --collect` — [x]
Runs one manual collection round (same as `sitt-collect`, NTES only unless Mobond is
permitted) and then recommends, so live readings are fresh. Never scheduled. **Done when:**
test with a mocked HTTP client (network stays blocked in tests); a failed collection still
gives a recommendation with a clear note.

### 10. How live delays drift (real-data analysis) — [x]
From the real NTES snapshots, measure how a train's reported delay changes over the next
15/30/45 min, and use the empirical spread to put a range ("±n min") on live predictions.
**Done when:** `docs/live-drift.md` with the numbers and caveats (one station, ~17 hours);
a reproducible command; the recommender's live range uses it or the doc explains why not.

### 11. Synthetic assumptions vs the first real readings — [x]
Compare the synthetic generator's delay distribution (by hour, fast/slow, direction) with
the real KYN readings. **Done when:** `docs/synthetic-vs-real.md` with a table and what, if
anything, to change in the generator; any clear, low-risk fix applied with tests.

### 12. Dashboard: data freshness and live readings — [x]
Show when the data was last collected and which trains have live readings.
**Done when:** dashboard tests cover it; app runs headless without errors.

### 13. Test coverage — [x]
Measure coverage (pytest-cov, free), list the weakest modules, add tests where they guard
real behaviour. **Done when:** coverage figure in PROGRESS.md and README dev section.

### 14. Independent review — [x]
A separate Claude Code agent reviews the round's changes cold; real findings get fixed.

## Round 3 (added 2026-10-12, self-directed)

### 15. Resolve the 95335 timetable mismatch — [x]
Check the official DOWN PDF (your PC, `scratch/cr-main-dn.pdf`) for 95335. **Done when:**
either an override in `timetable_overrides.toml` with the PDF evidence, or a note in
`docs/data-quality.md` saying the timetable matches the PDF and NTES differs.

### 16. Line-state nowcast, backtested on real data — [x]
When several trains in the same direction were just read as late, are trains without a
reading likely late too? Backtest on the real readings: for each run, predict its final
delay from (a) zero, (b) its own latest reading, (c) the median latest delay of other
same-direction trains read in the previous 30 min, and combinations. **Done when:** a
reproducible command and `docs/line-state.md` with the errors; the recommender uses the
signal only if it clearly beats the alternatives, with a note in replies.

### 17. Two-minute quick start — [x]
A README section that gets a new reader from clone to a `sitt-next` answer with the sample
timetable, verified by running it in a fresh clone.

### 18. Bot handler tests — [x]
Cover the untested `/log` button flow and the text-message paths in `bot/handlers.py`.

## Round 4 (added 2026-10-12, self-directed)

### 19. Is the timetable current? — [x]
News checked 2026-10-12: the 22 extra 15-car services of 15 Aug 2026 are in (the 15-car
supplement); the 12 services converted to AC from 29 June 2026 are **not** (our AC
supplement is from April 2025, and no newer one is on your PC). Recorded in the README.
Checked Central Railway's timetable page on 2026-10-13: the main-line AC supplement listed
is still the one of 16.04.2025, so there is no newer official source yet. Re-check when CR
publishes one, then re-run the converter (README, "Load the timetable").

### 20. Baseline and data-quality flags — [x]
The data-quality doc said the baseline ignored `dq_flags`; it doesn't (it reads the same
feature table). Proven with a test; doc corrected.

### 21. `/next … live` in the bot — [x]
`/next KYN CSMT live` runs one manual collection round first, like `sitt-next --collect`,
only when asked. **Done when:** handler test with a fake NTES; help text and docs updated.

## Round 5 (added 2026-10-13, at your request): an end product

### 22. Web app: backend and UI — [x]
`uv run sitt-web`: a FastAPI JSON API over the recommender (status, stations, recommend,
collect on demand, crowd reports) and a page with an indicator-board design, the next
trains with delay and crowding, live check, crowd logging and saved routes. Works offline
(fonts bundled). **Done when:** API tests pass; checked in a real browser at phone and
desktop widths, light and dark, against a copy of the real database; README and
`docs/web.md`; independent review.

## Round 6 (added 2026-10-13): next, all doable without the user

Do these in order; each is a PR merged on green CI with a PROGRESS.md entry. None needs the
user's PC: work in a cloud clone; a copy of the real database can be staged from the PC with
device_stage_files (`data\sitt.duckdb`), read-only.

### 23. CI on Windows too — [x]
The user runs Windows. Add a `windows-latest` job to `.github/workflows/ci.yml` (free for
public repos) running `uv sync --locked` and `uv run pytest`. Fix anything Windows-only
that breaks (paths, file locks, encodings). **Done when:** both jobs green.

### 24. Web app on a phone: installable, offline shell — [x]
Add a web app manifest and a small service worker so the page can be added to a phone's
home screen (with `sitt-web --host 0.0.0.0`) and opens even when the PC is briefly
unreachable, showing the last answer with a clear "not live" note. No external services.
**Done when:** Lighthouse-style checks pass in headless Chromium; tests for the manifest and
offline fallback; docs/web.md updated.

### 25. Delay patterns in the web app — [x]
A second view in the web app (`/patterns`): delay by hour and direction from real
observations, with the honest caveats from docs/live-drift.md; no Streamlit needed.
**Done when:** API endpoint + view + tests; screenshots at phone and desktop width.

### 26. Harbour line timetable — [ ] (blocked: needs the PDFs)
Central Railway now publishes Harbour line PDFs (w.e.f. 01.05.2026) on its timetable page.
docs/expansion-assessment.md says the converter reads the layout and fails only on station
names. Add the Harbour stations and convert; keep it behind a separate `line` so main-line
results don't change. **Done when:** converter tests on a fixture from the real PDF; a
Harbour route answers in `sitt-next` and the web app.
Blocked 2026-10-16: the cloud workspace can't reach cr.indianrailways.gov.in (the proxy
answers 403), and the copies on your PC (`scratch/part10/`) need folder access, which was
declined. **Needs you:** put `DN HB REVISED PTT WEF 01.05.2026.pdf` and the UP file in the
project folder (or connect the folder). Also note the size: docs/expansion-assessment.md
puts it at 5-6 days, most of it letting a station belong to more than one line.

### 27. Independent review of round 6 — [x]
A separate agent reviewed cold and found 5 issues, all fixed (PROGRESS.md, 2026-10-16).

## Round 7 (added 2026-10-16, self-directed)

### 28. Run grouping that doesn't depend on the computer's time zone — [x]
`live_drift.runs_ctes` groups a train's readings into one run with `date_trunc('day', ...)`
on a TIMESTAMPTZ, which follows DuckDB's session time zone: the same database can give
different runs on the PC (IST) and in CI (UTC) for trains near the 03:00 boundary. Use the
fixed +05:30 offset like the rest of the code. **Done when:** a test that sets a different
session time zone gets the same runs; drift, line-state and patterns numbers unchanged on the
sample data.

### 29. Saved answers don't show trains that have already left — [x]
Offline, a saved "leaving now" answer from hours ago still lists trains long gone. Drop
trains whose (predicted) departure has passed, say how many were dropped, and say so plainly
when none are left. **Done when:** browser check covers it; docs/web.md updated.

### 30. `sitt-patterns` on the command line — [ ]
The same tables as `/patterns`, printed in the terminal (and `--markdown` for docs).
**Done when:** CLI tests; README and docs mention it.

### 31. Independent review of round 7 — [ ]

### On the user's PC, only if a working shell is available
`mcp__remote-devices__device_bash` was blocked by a Windows update on 2026-10-12/13. If it
works in a later session: `git pull`, `uv sync`, start `uv run sitt-web --no-browser` in the
background of that shell, open http://127.0.0.1:8000 in Claude in Chrome, press "Check live
first" and record whether the real NTES fetch works. If it doesn't work, skip this; don't
use computer-use clicks to run scripts.

## Out of scope / needs you

- Asking Mobond for permission (the adapter stays off until then).
- Logging crowd reports and letting real readings accumulate (manual runs only).
- Refreshing the timetable when Central Railway publishes a new edition.
