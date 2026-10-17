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

### 26. Harbour line timetable — in parts
Central Railway publishes Harbour line PDFs (w.e.f. 01.05.2026) on its timetable page.
Split 2026-10-17, when the folder was connected: only the DOWN PDF is on your PC
(`scratch/part10/cr-harbour-dn.pdf`, saved for the 2026-10-05 assessment).

#### 26a. The converter reads the Harbour DOWN PDF — [x]
Harbour stations (codes checked against NTES and the GTFS feed; four unverified, listed in
docs/timetable-format.md), trains running beyond the table left out with a clear reason,
`--line harbour`. Main-line output unchanged (byte for byte on the real PDFs).

#### 26f. Independent review of the Harbour round — [x]
Nine findings; fixed in PRs #56-#58 (PROGRESS.md). What only matters once there are live
Harbour readings is listed in docs/expansion-assessment.md, "Known gaps".

#### 26b. The Harbour UP PDF — [ ] (blocked: needs the file)
**Needs you:** `UP HB REVISED PTT WEF 01.05.2026.pdf` from Central Railway's timetable page
(this workspace gets 403 there), in the project folder. Then convert it and add a fixture.

#### 26c. A station on more than one line — [x] (first part)
`station_lines` (code, line, seq) beside `stations`, whose line/seq are now the station's
home line: the main line whenever it is on it, in any load order. Main line answers
unchanged on a copy of the real database. Harbour trains load and answer `next_trains`.
The rest (routes, recommender, station lists) is 26d.

#### 26d. Routes and model features per line — [x]
Route points use each train's own line's places (the route model mixed up Dockyard Road
and Byculla, both 4th, once Harbour was loaded); only main line trains get pass points.
Megablock ends are placed on the block's own line; "train ahead" and the line median
only look at trains on the same line; the synthetic generator keeps to the main line.
Main line features and answers unchanged on the real data, with and without Harbour.

#### 26e. Harbour routes in the web app and bot — [x]
The train ahead (for crowding) is the one before on the same line; station lists put the
main line first, then the Harbour line's own stations; the status line names each line's
trains; README says how to load it. Checked in Chromium on the real data plus the Harbour
DOWN trains: CSMT → Panvel answers, the main line is unchanged.
The crowding rules (written for Kalyan-CSMT flows) apply unchanged: they only use
direction and time, which hold for the Harbour line too, but nothing has checked them there.

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

### 30. `sitt-patterns` on the command line — [x]
The same tables as `/patterns`, printed in the terminal (and `--markdown` for docs).
**Done when:** CLI tests; README and docs mention it.

### 31. Independent review of round 7 — [x]
Two low findings, both fixed (PROGRESS.md, 2026-10-16).

## Round 8 (added 2026-10-16, self-directed)

### 32. Accessibility check of both pages with axe-core — [x]
Run axe-core (free, from npm, injected by Playwright) on the main page and Delay patterns,
light and dark, phone and desktop, including the expanded train details and the offline
banner. Fix real violations (contrast, names, roles). **Done when:** a script reports no
serious or critical violations; colour-contrast fixes checked by eye in both themes.

### 33. Cancelled days counted by service day — [x]
The dashboard counts a train's cancelled days by Mumbai calendar date, so a cancellation
read either side of midnight counts twice. Use the same 03:00 service-day rule as runs.
**Done when:** a test with readings at 23:50 and 00:10 counts one day.

### 34. Quick start still works from a fresh clone — [x]
Re-run the README's two-minute quick start in a fresh clone (including `sitt-web` and
`sitt-patterns`) and fix anything that drifted. **Done when:** every command in it runs.

### 35. Independent review of round 8 — [x]
Four findings fixed (PROGRESS.md, 2026-10-16).

## Round 9 (added 2026-10-16, self-directed): using it from a phone

### 36. `sitt-web --host 0.0.0.0` prints the address to open on the phone — [x]
Today it prints 127.0.0.1, which a phone can't use. Print the PC's LAN address(es) (stdlib
only, no network calls) and a one-line hint. **Done when:** tests with a faked address
lookup; docs/web.md updated.

### 37. Offline, "Check live first" says why it can't work — [x]
Offline the button still looks usable and only explains itself after a press. Disable it
with a short reason while the app can't be reached, and enable it again when it can.
**Done when:** browser check covers both states.

### 38. Independent review of round 9 — [x]
Five findings fixed (PROGRESS.md, 2026-10-16).

## Round 10 (added 2026-10-16, for the next session)

### 39. Quiet the test client deprecation warning — [x]
Every test run warns that Starlette's TestClient over `httpx` is deprecated. Move to what
Starlette recommends (or pin) so real warnings stand out. **Done when:** `pytest` runs with
no warnings, on Linux and Windows CI.

### 40. Delay patterns: weekdays vs weekends — [x]
Rather than wait for the data, built now and shown only when the data allows: the switch on
`/patterns` and `sitt-patterns --days` appear once readings cover at least 2 weekdays and 2
weekend days or holidays; until then both say how many of each there are so far.

### 41. Independent review of round 10 — [x]
Done together with round 11's review (item 45): round 10's only code change, item 39, is
covered there.

## Round 11 (added 2026-10-17, self-directed)

No Harbour PDFs or folder access yet, and no new readings visible from the cloud, so these
are things that need neither.

### 42. Undo a crowd report logged by mistake — [x]
A mis-tap on the crowd meter stays in the database for good and skews that train's estimate.
Let the page take back its own reports for a short while (15 min); never the bot's or older
ones. **Done when:** API tests for allowed and refused cases; browser check of Undo, also
with the PC unreachable; axe clean.

### 43. Timetable and holidays still current — [x]
Re-checked 2026-10-17: no official 2027 Maharashtra holiday list yet (item 5 stays
blocked), and no Central Railway timetable or AC supplement newer than what item 19
recorded. Nothing to change.

### 44. How far the readings are from a real model — [x]
`sitt-retrain --check` knows how much real data a model needs (28 days, 5,000 readings…),
but only on the command line. Show the same progress on Delay patterns ("Readings on 3 of
the 28 days a delay model needs") so pressing **Check live first** has a visible point.
**Done when:** API field + page line + test; nothing is trained from the page.

### 45. Independent review of rounds 10-11 — [x]
Six low/medium findings, all fixed (PROGRESS.md, 2026-10-17).

## Round 12 (added 2026-10-17, self-directed, while you're away)

A parallel session finished the Harbour items (26c-26f, PRs #53-#58) while this one was
paused; nothing here repeats them.

### 46. Both sessions' work, end to end on the real data — [x]
On a copy of your database: 36 `sitt-next` answers (4 times, 9 main line routes, with
`--why`) byte for byte the same with the code before the Harbour work (PR #52) and after
(PR #58); after loading the Harbour DOWN trains, only routes between two stations both
lines serve (CSMT-Kurla, CSMT-Masjid) change, by listing Harbour trains too.

### 47. Say why there is no train — [x]
Panvel → CSMT and Kalyan → Panvel said "No trains found in the timetable for the next day".
Now: the stations are on different lines (and where they meet), or the line's timetable for
that direction isn't loaded, or no single train calls at both. Web page: only that sentence.

### 48. The quickest journey with one change — [x]
When the stations are on different lines, name the earliest-arriving journey with one
change where they meet, by the timetable (at least 5 min to change, at most 60 min wait),
instead of only "ask for each part separately".

### 49. Independent review of round 12 — [x]
Five findings (no high); three fixed, one noted (PROGRESS.md, 2026-10-17).

### 50. Ask each part of a journey with a change — [x]
The web page gives a button for each part of the suggested journey ("Kalyan → Kurla, 08:04"),
which asks for it from that train's time. `/api/recommend` gives the journey as data.

### 51. Independent review of item 50 — [x]
Seven findings (no high), all fixed; plus one from item 49's rules (PROGRESS.md).

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
