# PROGRESS

Newest first. Say "continue from PROGRESS.md" to pick up where this stops.

## Where this stopped (2026-10-17, 19:15)

**Everything is merged (PRs #46-#65).** Since this morning: round 11 (weekdays vs weekends,
Undo for crowd reports, progress towards a delay model), the Harbour line from its DOWN PDF
(converter, a station on two lines, routes, web app and bot: items 26a-26f, two sessions),
and round 12 (items 46-51: the real data checked end to end, "why there is no train", the
quickest journey with one change between lines and a button to ask each part, and two
review rounds' fixes).

**Checked on your PC at 19:13 (Saturday):** the new code is running, and one live check
gave 81 NTES readings, the first on a weekend: Delay patterns now has 3 weekdays and 1
weekend day (the Days switch needs one more weekend day, e.g. a check tomorrow), and the
delay model is at 4 of 28 days. The timetable reload below had **not** happened yet (the
database still had 894 main line trains and no Harbour line; no `sitt-before-reload`
copy): probably `sitt-web` was still running and held the database.

**When you're back at the PC** (stop `sitt-web` first; the reload needs the database to
itself). In the project folder, in PowerShell:

```powershell
git pull
uv sync
Copy-Item data\sitt.duckdb data\sitt-before-reload.duckdb
uv run python -m sitt.ingest.cr_pdf scratch\cr-main-dn.pdf scratch\cr-main-up.pdf --ac-supplement scratch\cr-ac.pdf --15-car-supplement scratch\cr-15car.pdf -o scratch\central.csv
uv run python -m sitt.ingest.timetable scratch\central.csv --replace
uv run python -m sitt.ingest.cr_pdf scratch\part10\cr-harbour-dn.pdf --line harbour --edition 2026-05-01 -o scratch\harbour.csv
uv run python -m sitt.ingest.timetable scratch\harbour.csv --line harbour --replace
uv run sitt-web --host 0.0.0.0
```

The first reload adds 95901 (T 15, the 08:04 AC fast from CSMT, 894 → 895 trains); the
second adds the Harbour DOWN trains (312), which is optional. Both were run on a copy of
your database: all 2,550 readings kept, main line answers unchanged. The copy made first
is there in case anything looks wrong.

What moves the project next, in order:
1. **More readings.** Press **Check live first** at different times over the next week or
   two, including a weekend. The weekday/weekend switch then appears by itself, and
   "Towards a delay model" shows how close `uv run sitt-retrain` is (3 of 28 days now).
2. **The Harbour UP PDF** (item 26b): `UP HB REVISED PTT WEF 01.05.2026.pdf` from
   https://cr.indianrailways.gov.in/view_section.jsp?lang=0&id=0,5,2360 into the project
   folder (this workspace gets 403 there). Then trains towards CSMT on the Harbour line,
   and changes like Kalyan → Chunabhatti via Kurla, work.
3. **Mobond permission**, for live CSMT-Kalyan, Harbour and Western locals
   (docs/data-sources.md); without it Harbour gets timetable times only.
4. Item 5 (2027 holidays) once Maharashtra publishes the list, usually Nov-Dec.

## 2026-10-17

### Item 51: independent review of item 50 (done, PR #64)
A cold review of PR #63 (on the real data, in Chromium) found no Python problems and seven
in the page, all fixed:
1. Offline, a part button replaced the saved answer and the trip with "no saved answer".
   Part buttons now need the PC: a saved ("Not live") answer doesn't show them.
2. One tap replaced your remembered route and the address with the part, and there was no
   way back. A part isn't kept or put in the address any more, and "← Back to Kalyan →
   Panvel" returns to the whole trip as asked (leaving now, or the time you gave).
3. "Another time" stayed set after a part, so a saved route chip later asked about a past
   time. Chips now always ask "leaving now".
4. Keyboard focus was lost after pressing a part; it moves to the new answer.
5. Buttons didn't say the day ("00:11" for a train after midnight) and weren't grouped;
   they now say "00:11 on Tue, 20 Oct", have spoken names, and sit in a labelled group.
6. Saved answers showed parts that had left: covered by 1.
7. docs/web.md now says all this. Also: fields are filled as the station list writes them
   (no "CSMT (CSMT)").
And one from item 49's rules: Dockyard Road → Byculla was offered out to Kurla and back in
on the main line. When the direct way from the origin needs the missing Harbour direction,
a journey away from CSMT and back isn't offered (test fails on the old code).
Browser checks for 1-5 (fail on the old page); axe clean on the real data with the part
buttons and with Back, light and dark.

### Item 50: ask each part of a journey with a change (done, PR #63)
- When the page suggests a journey with one change, it now shows "Ask each part:" with two
  buttons, "Kalyan → Kurla, 08:04" and "Kurla → Panvel, 08:53". Each fills in the trip and
  asks from that train's time, so its delays and crowding show (on the real data: "Take the
  08:04 fast: arrives 08:46…"). `/api/recommend` gives the journey as `change`.
- Browser check (stubbed answer, so it runs on the sample data): the buttons, the second
  part asked with `from=TNA&to=CSMT&at=08:53&date=…`, and none on an ordinary answer.
  Checked by hand on the real data with Harbour loaded, light and dark; axe clean. 1 API
  test.

### Item 49: independent review of round 12 (done, PR #61)
A cold review of PRs #59-#60 on the real data (Harbour DOWN loaded) found five issues, none
high. Fixed:
1. MEDIUM: Kalyan → Chunabhatti (also → Vadala Road, Thane → GTB Nagar) was routed into
   CSMT and back out and called "quickest": the natural change at Kurla needs Harbour
   trains towards CSMT, which aren't loaded. Now a journey whose first train passes such a
   station before its change isn't offered, and the reply says the Harbour line has no
   trains towards CSMT loaded.
2. MEDIUM: Panvel → Kalyan said "ask for each part separately", but the Harbour part can't
   be answered. It now says why.
3. LOW: Kurla → Bandra and Bandra → Vashi said only "No single train…"; they now add that a
   change may need Harbour trains towards CSMT, which aren't loaded.
5. LOW: a second train after midnight is marked "after midnight".
Noted, not changed: (4) a no-train answer runs about 24 timetable queries (0.6 s on the
real data), only when there is no train at all. Kalyan → Panvel, Thane → Vashi and the docs'
example are unchanged. 1 new test and 2 updated (fail on the old code); docs/recommender.md
says so.

### Item 48: the quickest journey with one change (done, PR #60)
- Kalyan → Panvel (real timetable, Monday 08:00) now says: "Quickest with one change on
  these two lines, by the timetable: the 08:04 fast, changing at Kurla (arrives 08:46) to
  the 08:53 slow, at Panvel 09:45. Ask for each part to see delays and crowding." Thane →
  Vashi changes at Kurla too; late at night the times run past midnight correctly.
- Rules: the first 6 trains to each station both lines call at; at least 5 minutes to
  change (a guess), at most 60 minutes of waiting; no ladies' specials; earliest arrival,
  then latest departure. The day is named when it isn't today ("the 04:15 slow tomorrow").
  Both limits were added after the fixture data suggested a 9-hour wait at CSMT and a
  04:15 train without saying it was tomorrow.
- Known limit, in docs/recommender.md: only the Central and Harbour lines are known, so
  Dadar → Goregaon goes via Sandhurst Road rather than the Western line.
- 2 new tests (fail on the old code).

### Item 47: say why there is no train (done, PR #59)
- Found in item 46: with the Harbour DOWN trains loaded, Panvel → CSMT and Kalyan → Panvel
  answered "No trains found in the timetable for the next day", which is wrong about the
  why. Now `sitt-next`, the web app and the bot say: "No train runs from Kalyan to Panvel
  without a change: Kalyan is on the Central line, Panvel on the Harbour line. They meet at
  CSMT, Masjid, Sandhurst Road and Kurla: ask for each part separately."; "The Harbour
  line timetable loaded here has no trains towards CSMT … only its DOWN timetable is
  loaded"; or, for Bandra → Vashi (two branches), "No single train in the timetable runs
  from Bandra to Vashi."
- The web page, with no train at all, shows only that sentence: the "Timetable times"
  line, the empty list's heading and "Why this train?" are hidden (browser check, fails on
  the old page). Checked on the real data in Chromium; axe clean. 5 tests, failing on the
  old code. docs/recommender.md says so.

### Item 46: both sessions' work, end to end on the real data (checked)
- On a copy of your database, the same 36 `sitt-next --why` answers (9 main line routes,
  4 times including last night's live readings) came out byte for byte the same from the
  code before the Harbour work (PR #52) and after it (PR #58).
- After loading the Harbour DOWN trains into that copy, only CSMT-Kurla and CSMT-Masjid
  answers changed, by listing Harbour trains too, which do serve those trips. Harbour
  routes answer (CSMT → Panvel: the 08:00 slow, arrives 09:20).

### Review of the Harbour round: fix 3 of 3 (PR #58)
- MEDIUM: a Harbour CSV loaded without `--line harbour` went in as the main line and moved
  main line places (Thane 19 → 50). The converter now writes `# line: harbour` (or
  `central`) and the loader refuses a file for another line.
- MEDIUM: station order put stations no train relates by code, so branches interleaved
  (the Harbour's Panvel and Goregaon branches; the main line's beyond Kalyan). A Kurla-Vashi
  block then covered Bandra. Each branch is now listed whole. Real timetables: CSMT to
  Kalyan places unchanged; all 35 model features identical on the real readings (same
  CSV loaded with old and new code); `sitt-next` unchanged.
- LOW: loading the Harbour line renamed CSMT to its Harbour spelling; a station's name now
  comes from its home line.
- Docs: the expansion assessment's status corrected, plus "Known gaps once there are
  Harbour readings" (line-state backtest, dashboard order, 91xxx trains read as Western,
  blocks across a junction).
- Noticed on the way: your database has 894 main line trains; converting the same PDFs
  now gives 895. The missing one is 95901 (T 15, the 08:04 AC fast from CSMT), which the
  converter learned to read (a stray mark in one cell) after your database was loaded.
  Reloading the timetable adds it (see "Where this stopped").
- 6 new tests, all failing on the old code.

### Review of the Harbour round: fix 2 of 3, crowd reports per line (PR #57)
- HIGH: at stations both lines share, a report like "08:00 slow from CSMT" was matched
  against Harbour trains too (on the real timetable: to the 08:00 to Panvel instead of the
  07:58 to Thane, or to nothing, "two trains too close to tell apart"). The matcher now
  looks at one line: the one the report names, else the station's home line (the main
  line at CSMT, Masjid, Sandhurst Road and Kurla), which is exactly what it did before.
- HIGH/MEDIUM: Harbour crowd reports raised main line crowding at shared stations (4
  reports of "can't board" on a Harbour train took the 18:03 to Thane from 3 to 4). Only
  reports about the trip's line count now.
- New nullable `crowd_reports.line`. The web app sends the train's line when you log from
  a train's row (checked in Chromium on the real data with Harbour loaded: logging from
  the 15:44 to Panvel sent "harbour"; editing the time by hand drops it); `/api/recommend`
  gives each option's `line`. A line that doesn't call at the station is refused (400).
  Bot reports carry no line, so they are about the main line at shared stations.
- 3 new tests (the matching and similar-reports ones fail on the old code).

### Review of the Harbour round (items 26a-26e): fix 1 of 3 (PR #56)
A cold review of PRs #51-#55 found nine issues (two high). This PR fixes:
- HIGH: on a database not yet opened for writing since `station_lines` was added (the
  PC's, right after a pull), `sitt-next` and the web app's recommendations crashed: they
  open the database without applying the schema. Places now come from `station_lines`
  when it has rows and from `stations` otherwise. Checked on an untouched copy of the real
  database: `sitt-next` answers and `/api/recommend` returns 200.
- LOW: the one-time copy into `station_lines` ran on every schema apply, so a station
  dropped from a line by a `--replace` load came back. It now runs into an empty table
  only.
3 new tests, all failing on the old code.

### Item 26e: Harbour routes in the web app and bot (done, PR #55)
- CSMT to Kurla is served by both lines. The crowding rule's "train ahead" was simply the
  previous train listed, so a cancelled Harbour train made the next main line train look
  more crowded. It is now the previous train on the same line (test fails on the old code
  with "the train before it is cancelled").
- Station lists (web `/api/stations`, the bot's directory, the dashboard) put the main line
  first and then the Harbour line's own stations, instead of interleaving the lines'
  places (Dockyard Road after Byculla, both 4th). `/api/stations` gives each station's
  `line`; `/api/status` gives `lines` (trains per line), and the page's status line says
  "Central line timetable, 894 trains; Harbour line, 312." (unchanged with one line).
- Checked in Chromium on a copy of the real database with the Harbour DOWN trains loaded:
  CSMT → Panvel answers ("Take the 15:08 slow, arrives 16:28"), Kalyan → CSMT unchanged;
  offline, patterns and axe checks pass. README says how to load the Harbour line.

### Item 26d: routes and model features per line (done, PR #54)
- Found by loading the Harbour DOWN trains beside a copy of the real database: the route
  model mapped place numbers to stations across all lines, so once Harbour was loaded,
  "4th station" could be Dockyard Road instead of Byculla, giving main line fast trains
  pass points at Harbour stations (and Harbour trains main line points). Route points now
  use the train's own line's places (`station_lines`), and only main line trains get
  pass points.
- Model features: megablock ends are placed on the block's own line; the "train ahead"
  and line median only use trains on the same line. The synthetic generator models the
  main line only.
- Real data: all 35 feature columns identical over 5,020 rows before and after, and again
  with the Harbour line loaded beside it; `sitt-next` answers unchanged. With Harbour
  loaded, `sitt-next CSMT PNVL` and `CLA VSH` answer from the timetable.
- 5 new tests (3 fail on the old code; the block-placement and whole-feature-table ones
  pass on it too, because the shared stations happen to have the same places on both
  lines in the fixture: they pin the behaviour down for when they don't).

### Item 26c: a station on more than one line (done, PR #53)
- New `station_lines` table: each line a station is on and its place there, recomputed by
  the loader from that line's trains. `stations.line`/`seq` are now the home line, the
  main line whenever a station is on it, in any load order (before, loading the Harbour
  line would have moved CSMT, Masjid, Sandhurst Road and Kurla to it).
- Older databases get `station_lines` copied from `stations` when the schema is applied.
  On a copy of the real database: 51 stations copied, all matching, and `sitt-next`
  answers byte for byte the same before and after.
- Tests with real Harbour trains from the fixture: main line places and answers unchanged
  after loading the Harbour line; both load orders give the same tables; reloading one
  line keeps the other's places; CSMT to Panvel answers (98001, 00:13). 4 new tests, all
  failing on the old code.

### Item 26a: the converter reads the Harbour DOWN PDF (done, PR #51)
- With the folder connected: no new readings (the database was last written at 23:22 on
  the 16th). The real copy shows this morning's features working: 180 trains over 3
  weekdays and no weekend days, so the Days switch stays hidden and says so; the delay
  model is at 3 of 28 days. Only the Harbour **DOWN** PDF is on the PC.
- Converter: 31 Harbour stations added (codes from NTES's station list and the GTFS feed;
  King's Circle, Mahim, Santacruz and Goregaon only from erail.in, marked unverified;
  "Ramnagar" in the PDF is Ram Mandir). `--line harbour` for the CSV header.
- New rule: a station code in a cell for a station that isn't a row of the table means
  the train runs beyond it, with that station's time in the next row. Such trains are
  left out with a reason instead of the next row's time being read as a stop: the 37
  Trans-Harbour trains from Thane ("TNA" in the CSMT row, joining at Nerul) and 91491
  (to Borivali). "L/SPL" is read as the ladies' special; 98193 is still left out for an
  unexplained "#".
- Result: 312 of 351 DOWN trains, all slow, checked against the rendered page 1. The main
  line converts byte for byte as before (CSV and report) from the real PDFs.
- Fixture from real pages 1, 13 and 16; 5 new tests, failing on the old code.
- Loading is item 26c (a station on two lines); the UP PDF is item 26b and needs you.

### Item 45: independent review of rounds 10-11 (done, PR #49)
A separate agent reviewed PRs #46-#48 cold (time zones, the DELETE endpoint's limits and
SQL, offline behaviour, resize, cost all checked fine). Six findings, all reproduced, all
fixed:
1. The caveat ignored the Days switch: on the weekend view it still said "300 trains over
   14 days" and could drop the "Early days" warning. It now follows the view.
2. Any unexpected error in the new model section (e.g. a database without
   `model_registry`) made `/api/patterns` fail with 500. The table is now checked, and the
   section is left out (and logged) rather than breaking the page.
3. "Readings on N days" counted only days with trains seen (runs). Now worded "trains seen
   on…"; docs say the model gates count readings, so their day count can be higher.
4. A double tap on Undo sent two requests and ended on "No such report". The button is
   disabled while its request runs.
5. After a failed Undo, focus jumped to the heading although the report was still there;
   now it stays on (or returns to) that report's Undo button.
6. Switching Days was silent for screen readers; the summary line is now `aria-live`.
Also: docs say where the holidays come from (`holidays.toml`, tentative dates) and that
Saturday counts as weekend on purpose. Browser checks for 1, 4, 5, 6 and tests for 2, 3
fail on the old code and pass now.

### Item 44: progress towards a delay model on Delay patterns (done, PR #48)
- New **Towards a delay model** section: the `sitt-retrain` gates (days, usable readings,
  best-covered station, test window) as "Days with observations: 6 of 28", or which
  real-data model is in use. Uses `check_readiness` read-only; nothing is trained. Synthetic
  databases show nothing (sitt-retrain refuses them). `/api/patterns` adds `model` when
  there are readings; `sitt-patterns` ends with the same line.
- Browser check (`check_web_patterns.py`) and axe: pass, no violations. 4 new tests.

### Item 43: timetable and holidays still current (checked, no change)
- Web search 2026-10-17: no official 2027 Maharashtra holiday list yet; the latest Central
  Railway news is still the 12 AC conversions of 29 June, already recorded (item 19).

### Item 42: undo a crowd report logged by mistake (done, PR #47)
- Reports logged from the page get an **Undo** button in the list below the form for 15
  minutes (`DELETE /api/reports/{id}`; the list says `can_undo`). Older reports and the
  bot's are refused (409) and never removed from the page. Taking one back removes it from
  the crowding estimate at once (tested).
- If the PC can't be reached, the report stays and the page says so. After Undo, keyboard
  focus moves to the section heading (the button is gone).
- Browser check (`check_web_offline.py`): log, Undo while unreachable, Undo; phone and
  desktop, light and dark. The a11y check now audits the main page with an Undo button:
  no violations. 2 new API tests.

### Item 40: weekdays vs weekends in Delay patterns (done, PR #46)
- Built now, shown only when the data allows, so it turns itself on as readings come in:
  once there are readings on at least 2 weekdays (Mon-Fri) and 2 weekend days or holidays
  (Sat, Sun, and the Sunday-timetable holidays in `sitt.holidays`), `/patterns` gets a
  **Days** switch (All days / Weekdays / Weekends & holidays, kept in the address as
  `?days=weekend`) and `sitt-patterns --days weekday|weekend` works. Before that, the
  page's caveat and the end of `sitt-patterns` say how many of each there are so far.
- `/api/patterns` adds `day_kinds`, `split_ready`, `split_min_days` and `by_kind`; each
  section has its own runs, days, dates and stations.
- Fixed on the way: Delay patterns counted days by calendar date, so a train due at 00:30
  added a day; it now uses the 03:00 service day, like runs. Test fails on the old code.
- New `scripts/check_web_patterns.py` (Chromium): hidden with one weekend day, and with
  two it changes the numbers, keeps the choice across a reload, no sideways scrolling, at
  390/1280 px light/dark. The a11y check now uses a week of invented readings and audits
  the weekend view: no violations. 5 new tests; the 4 for the new behaviour fail on the
  old code.

## 2026-10-16

### Tests could reach Central Railway's site (fixed, PR #44)
- Seen in the cloud workspace's proxy log: a full `pytest` run made one request to
  cr.indianrailways.gov.in. Cause: the dashboard's Streamlit test harness can leave a thread
  running after a test, after the test's `SITT_DASHBOARD_DOWNLOAD=0` is undone and between
  tests, where the network guard isn't in place, so it tried to download the timetable PDFs.
  With a proxy configured the guard also couldn't see the real host (clients connect to the
  proxy on this machine).
- Now `tests/conftest.py` turns dashboard downloads off for the whole run and strips proxy
  settings in every test. Verified: the request appears with the old setup and not with the
  new one. On your PC this would have been a real download during tests.

### Review of the UI pass (PR #43)
Checked again on the PC at 22:30 (112 readings; the recommended 22:02 fast was 39 min late,
leaving 22:41: the new leaving-order list showed it correctly). A cold review found five
issues, all fixed:
1. "Take" vs "Wait for" was decided by timetable order. Now by expected departure: a late
   train leaving after the pick isn't waited through, and an on-time train leaving before a
   late pick is. One old test encoded the wrong wording ("Wait for the 08:25" while the late
   08:12 left after it); corrected, plus a new test. docs/recommender.md updated.
2. The folded trip line kept naming the old trip after an offline answer or failure; now it
   follows the saved answer, and the form opens when nothing could be shown.
3. With a date and no time the line said "leaving now"; it now uses the time really asked
   ("from 00:00 tomorrow").
4. Pressing Enter in a field that then folds away dropped keyboard focus; it moves to Change.
5. "On time every hour" dropped the hours where some trains were late; the note lists them.
Browser checks for 2-4 fail on the old code and pass now; axe: no violations.

### UI pass on real data (PR #42)
- Checked the running app on the PC through Chrome: real timetable (894 trains), live check
  worked (123 NTES readings). Real finding: trains **towards CSMT** reach Kalyan a median
  6-17 min late (52-74% 5+ min late by part of day); trains **away from CSMT** are on time.
- Main page: trains listed in the order they will **really leave** (the 20:34 running 36 late
  now sits after the 21:08, shown as 21:10 in red with 20:34 struck); the board says
  `EXP 21:10` for late trains and picks its next rows by expected time; on phones the form
  folds to one line once there's an answer, so the board is in view; the four notes moved
  into "About these times" (opened automatically offline when trains have left).
- Delay patterns: opens with each direction as a board line ("TOWARDS CSMT 10 LATE") and a
  sentence; a direction that is on time every hour gets a sentence, not a flat chart. New
  `by_direction` in `/api/patterns` (tested).
- Re-rendered with the real numbers at 390/1280 px, light/dark; browser check and axe pass.

### Item 39: no more test warning (done, PR #41)
- Starlette 1.7's TestClient wants `httpx2`; added as a dev-only dependency (`uv add --dev`),
  so tests run with no warnings and a new warning will stand out. The app itself still
  uses `httpx`; `requirements.txt` (runtime only) is unchanged.

### Item 38: independent review of round 9 (done, PR #40)
Five findings, all fixed:
1. MEDIUM: if the PC answered but its status check failed (e.g. database busy), the live
   button stayed on "needs the PC". Now any answer from the PC clears the offline state.
2. The live button could be re-enabled mid-request (a status check finishing during a
   slow answer). A single `busy` flag now governs it.
   Both are covered in `check_web_offline.py`, run without a service worker (Playwright
   can't intercept a worker's requests: the first version of these checks passed on the old
   code for that reason; now they fail on it and pass on the fix).
3. Windows: a VPN's address could be printed first and WSL/Hyper-V adapters (172.16-31.x)
   offered to the phone. Now 192.168.x first, others as "if that doesn't load, try…",
   virtual 172.x adapters dropped when there is anything better.
4. `--host ::` printed `http://:::8000/`; now `http://[::1]:8000/`, with the phone hint and
   a note that `::` may be IPv6-only on Windows.
5. A host name too long for the lookup raised UnicodeError; now skipped like OSError.

### Item 37: offline, the live button says it needs the PC (done, PR #39)
- While the app can't be reached, "Check live first" is disabled and reads "Live check
  needs the PC" (dashed, not faded, so it stays readable), with a tooltip saying why. It
  comes back as soon as an answer arrives from the PC. One place now sets its state.
- Browser check: offline state, then the server is restarted on the same port and the
  page asked again: button back, "Not live" note gone. axe: no violations offline.

### Item 36: `sitt-web --host 0.0.0.0` prints the phone address (done, PR #38)
- Prints e.g. "On your phone (same Wi-Fi), open http://192.168.1.23:8000/", from local
  lookups only (the computer name's addresses and the route the system would use; a UDP
  "connect" sends nothing). Only home-network ranges (10/8, 172.16/12, 192.168/16), so
  loopback, link-local and documentation addresses are never suggested. Falls back to the
  `ipconfig` hint; mentions the Windows firewall prompt. 5 new tests.
- Not tried on the real PC (no shell there); the cloud container has no home network, so
  it printed the fallback, as expected.

### Item 35: independent review of round 8 (done, PR #37)
Four findings, all fixed:
1. MEDIUM: the offline board was dimmed with a CSS filter, which took its text to about
   3:1, and axe-core can't see filters. Now the board keeps full colours and gets a dashed
   amber frame; the "Not live" banner says the rest.
2. The hourly chart was `role="img"` with labelled bars inside; an image's children are
   presentational, so spec-following screen readers could drop the bar labels. The chart
   is now `role="group"`; empty hours are `aria-hidden`.
3. The a11y script dropped axe's "incomplete" results and never opened the patterns
   page's details. It now lists them as "check by hand" (only axis labels and text under
   the tooltip, all 6:1+ by hand) and audits with details open and with a tooltip showing.
4. Cancelled days used a fixed 03:00 cut, while the same table's `days` uses each run's
   start time. Now both use the run's start (test fails on the 03:00 rule).

### Item 34: quick start re-checked in a fresh clone (done, PR #36)
- Fresh `git clone` + `uv sync`: every quick-start command runs as written (`sitt-init-db`,
  loading the sample, `sitt-next KYN CSMT --at 07:00` with the documented output,
  `--train 07:12 --why`, `Kalyan Thane`); `sitt-web` serves `/`, `/patterns`, `/sw.js`,
  the manifest and the APIs; `sitt-patterns` says there are no real readings yet (exit 1).
- README now says Delay patterns stays empty with the sample data.

### Item 33: cancelled days by service day (done, PR #35)
- The dashboard's per-train "cancelled days" now uses the 03:00-03:00 Mumbai service day,
  like runs: a cancellation read at 23:50 and again at 00:10 is one day, not two. Test fails
  on the old code. The recommender's own cancellation check uses a time window and was
  already fine.

### Item 32: accessibility check with axe-core (done, PR #34)
- `scripts/check_web_a11y.py` (axe-core 4.10.3 from npm, injected by Playwright): main page
  with details open, offline view, Delay patterns; 390/1280 px; light/dark.
- First run: 12 serious findings in 4 kinds, all fixed: board's later rows 2.3:1 (now
  `#b07d20`, 4.9:1); light-mode green "on time" 4.2:1 on the page (now `#126b42`, 5.6:1);
  the offline view's 80% opacity on the train list dropped contrast (removed; the banner
  and greyed board say "not live"); chart bars had aria-label without a role (now
  `role="img"`). Two moderate landmark issues fixed too. Now: no violations at any level.
  Checked by eye in both themes.

### Item 31: independent review of round 7 (done, PR #33)
A cold review (pytest under UTC/IST/Los Angeles, Chromium probes of `render()`) found no
problem with the time-zone change and two low ones, both fixed:
1. Offline, a saved answer whose trains were all cancelled showed an empty board (online it
   says NO TRAIN): the "pick has left" branch ran for every saved answer. Now only when the
   pick really has left and something is still running. Re-checked with the reviewer's
   probe: NO TRAIN offline and online.
2. `sitt-patterns` rounded percentages half-to-even (1 in 8 = 12%) while the page shows 13%.
   Now half up, with a test.

### Item 30: `sitt-patterns` (done, PR #32)
- `uv run sitt-patterns [--db] [--markdown]`: the Delay patterns page as text (summary line,
  part of day by direction, fast vs slow; `*` marks cells under 5 trains). Exit 0 with data,
  1 with no real readings, 2 with no database. Rounds half up like the page.
- README command table and layout updated (the `scripts/` line was also out of date).

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
