# The web app

`uv run sitt-web` starts the app on your computer and opens it in your browser at
<http://127.0.0.1:8000>. It is the easiest way to use the project: pick where you're going,
see which train to take, and log how crowded it was.

```bash
uv run sitt-web                      # http://127.0.0.1:8000, opens the browser
uv run sitt-web --port 8080 --no-browser
uv run sitt-web --host 0.0.0.0       # also reachable from your phone on the same Wi-Fi
```

It runs until you stop it with Ctrl+C. **Nothing runs in the background**: live status is
fetched only when you press **Check live first**, exactly like `sitt-next --collect`. That
takes a few seconds, and other requests to the app wait until it is done.

## What the page does

- **Pick a trip.** From and To accept a name or a code (`Kalyan`, `KYN`); mistyped stations
  get "Did you mean…?". **Another time** asks about a later time or another day; leave the
  date empty and a time earlier than now means tomorrow (at 7 pm, 08:00 is tomorrow
  morning). On a phone, once there is an answer the form folds into one line ("Kalyan →
  CSMT, leaving now", **Change**) so the board is in view.
- **The board** at the top shows the recommended train the way a platform indicator would:
  time, destination, fast or slow, coaches, and whether it is late. Under it, the one-line
  reason and what the times are based on (timetable, past delays, live readings).
- **The next trains** are listed in the order they are expected to leave. A late train shows
  its expected leaving time in red with the timetable time struck through (and the board
  says `EXP 21:10`, like a platform board). Each has its predicted arrival and delay, a
  five-step crowding meter, and tags (AC, 15-car, ladies' special). Tap a train for the details, or to log
  crowding for it.
- **Why this train?** shows the full explanation, the same as the bot's `/why`.
- **How crowded was your train?** saves a crowd report. It is matched to a scheduled train
  and adjusts the crowding estimate, the same as the bot's `/log`. Tapped the wrong level?
  A report logged from the page has an **Undo** button in the list below the form for 15
  minutes; after that, and for reports sent from the bot, it stays (they can only be removed
  in the database itself).
- **Save this route** keeps your usual trips as one-tap buttons (stored in this browser).

## Delay patterns

**Delay patterns** (`/patterns`, linked at the top of the page) opens with each direction as a
line on a platform board ("TOWARDS CSMT  10 LATE") and the same in words, then shows how
late trains have really been: the median delay and the share of trains 5+ minutes late by part of the day
(early morning, the two peaks, midday, night) and hour by hour, for each direction, plus
fast against slow. Hover or tab to a bar for its numbers.

Each train counts once per station and day, with the delay of its **last** reading, and
only if that reading was taken within 15 minutes of the train's time: the same rule as
[`live-drift.md`](live-drift.md), because earlier readings often say "on time" for trains
that end up late. Only real readings of suburban trains are used, never synthetic data.
Hours or parts of the day resting on fewer than 5 trains are drawn pale and marked "too few
to go by", and a note at the top says how many trains, days and stations it all rests on.
With no real readings yet the page says so and how to collect some.

**Weekdays and weekends.** Once trains have been seen (runs, as above) on at least 2
weekdays (Monday to Friday) and 2 weekend days or holidays, a **Days** switch appears above
the board: All days, Weekdays, Weekends & holidays. Weekend means Saturday, Sunday and the
holidays on which the Sunday timetable runs (the fixed dates in `src/sitt/holidays.py`, the
movable ones in `src/sitt/holidays.toml`; tentative dates only with
`SITT_HOLIDAYS_INCLUDE_TENTATIVE=true`). Saturday counts as weekend although it runs the
weekday timetable: fewer people travel, and the question is "weekdays or weekends". The
choice is kept in the address (`/patterns?days=weekend`), so it survives a reload or a
bookmark, and the summary line and caveat follow it (the summary is announced to screen
readers). Before that the caveat says how many days of each have trains seen so far; with
fewer a split would only show noise. Days with readings only long before a train's time
don't count. A train after midnight belongs to the day before (the 03:00 service day), so
the 00:30 early on a Saturday counts as Friday. In the terminal: `uv run sitt-patterns
--days weekday` (or `weekend`); without `--days` the report ends with the same sentence.

**Towards a delay model** at the bottom shows how far the real readings are from what
`uv run sitt-retrain` needs before it trains a delay model (28 days, 5,000 usable readings
and so on, the same gates as `sitt-retrain --check`; [`retraining.md`](retraining.md)),
counted over every day whatever the Days switch shows, or which real-data model is in use.
These gates count readings, not runs, so "Days with observations" can be higher than the
days in the summary. Nothing is trained from the page, and if this section can't be worked
out the rest of the page still shows. `sitt-patterns` ends with the same line.

It describes the past; the recommender does the predicting. Code: `src/sitt/patterns.py`.
The same tables in a terminal: `uv run sitt-patterns` (`--markdown` for docs).
The Streamlit dashboard's delay heatmaps count every reading instead, so a train seen four
times weighs four times as much there.

## On your phone

Start the app with `uv run sitt-web --host 0.0.0.0`. It prints the address to open on your
phone (same Wi-Fi), such as `http://192.168.1.23:8000/`, worked out on the PC itself
without contacting anything. It is a best guess: home Wi-Fi addresses (192.168.x.x) come
first, a VPN's after them as "if that doesn't load, try…", and Windows' virtual adapters
(WSL, Hyper-V) are left out. If none works, `ipconfig` on Windows shows the Wi-Fi
adapter's IPv4 address. Use `0.0.0.0` rather than `::`, which on Windows may accept IPv6
only. The first
time, Windows may ask whether Python may accept connections: allow it for private
networks, or the phone won't load the page. Chrome's menu
has **Add to Home screen**, and Safari's share sheet has **Add to Home Screen**: that puts
an icon on the home screen that opens the page. Over plain `http://` to the PC's address,
Chrome treats it as a shortcut, so it opens in a normal browser tab; opening full screen as
an installed app needs `https://` (or `localhost`, on the PC itself). Not tried on a real
phone yet.

**When the PC can't be reached** (it is asleep, `sitt-web` isn't running, or you've left the
Wi-Fi), the page shows the last answer it got for that trip, marked **Not live** with the
time it was saved, and the header says it is offline. The phone keeps the last 8 answers.
Trains that have left since the answer was saved (predicted departure more than a minute
ago) are dropped, with a note saying how many; if the recommended train has left, the page
says so and lists the saved trains still to come, and if all have left it says that too.
A trip with no saved answer says so. **Check live first** turns into a dashed, disabled
"Live check needs the PC" (the PC does the fetching), and comes back the next time you ask
for trains and the PC answers (or when the phone reconnects; nothing checks in the
background). Logging crowding needs the PC; the page tells you the
report wasn't saved. Nothing is fetched or refreshed in the background: answers are saved
only when you ask for trains.

**What can open with the PC off depends on the address.** Browsers only let a page keep its
own files (a *service worker*) on `https://` or `localhost`:

| Opened as | Page opens with the PC off | Saved answers shown when the PC drops |
| --- | --- | --- |
| `http://127.0.0.1:8000` on the PC itself | yes | yes |
| `http://192.168.x.x:8000` on a phone | no: the browser shows its own "can't connect" page | yes, while the page is already open |

So on a phone over plain Wi-Fi the useful part is the second column: open the page at home,
and if the PC goes away while you're on the platform, the answer stays readable and is
labelled as not live. Serving the app over `https` on your home network would allow the
first column too, but needs a certificate the phone trusts; that isn't set up.

The manifest is at `/manifest.webmanifest`, the service worker at `/sw.js`. The worker keeps
only the two pages (the main page and Delay patterns), their styles, scripts, fonts and
icons, the station list and the status line. Any other page you open (the API docs, raw
JSON) is never kept, and offline it falls back to the main page. Answers are kept by the
page itself in the browser's storage. The pages load their styles and scripts as
`?v=<hash of the static files>`, so after an update a phone never runs the new page with
an old script it kept; restart `sitt-web` after changing the page. Delay patterns needs the
PC: offline (if you opened it before) it says it can't reach the app. Icons are drawn by
`scripts/make_web_icons.py`.

To check all of this in a real browser:

```bash
python3 scripts/check_web_offline.py          # needs Playwright + Chromium for that python3
```

It builds a throwaway database from the sample timetable, starts `sitt-web`, checks that
Chrome finds the app installable and that the worker controls the page, stops the server,
reloads, and checks the "Not live" fallback, that trains drop out of it as the clock moves
past them, and that opening other pages (the API docs, raw
JSON) never replaces the kept main page, at phone and desktop width in light and dark,
saving screenshots to a temp folder. Checked 2026-10-16: all checks pass.

It also logs a crowd report and takes it back with **Undo**: when the PC can't be reached
the report stays, with a message, and keyboard focus stays on its Undo button; a double tap
sends one request. Checked 2026-10-17: all checks pass.

`scripts/check_web_patterns.py` checks the Delay patterns weekday/weekend switch the same
way, on two invented databases: with one weekend day the switch stays hidden and the caveat
says what is missing; with two, the switch changes the numbers, keeps the choice in the
address and across a reload, at phone and desktop width in light and dark. Checked
2026-10-17: all checks pass.

## Accessibility

`scripts/check_web_a11y.py` runs [axe-core](https://github.com/dequelabs/axe-core) (free, from
npm) on the main page with a train's details and "Why this train?" open, on the offline
"Not live" view and on Delay patterns (all days and the weekend view), at phone and desktop
width, light and dark:

```bash
npm install --prefix /tmp/axe axe-core@4.10.3
python3 scripts/check_web_a11y.py --axe /tmp/axe/node_modules/axe-core/axe.min.js
```

Checked 2026-10-17 (with the weekend view and an Undo button in the reports list): no
violations. axe also lists what it couldn't decide ("check by
hand"): on Delay patterns that is the chart's axis labels and text under the tooltip, all
6:1 or better worked out by hand. Its first run found the board's later rows (2.3:1) and
the green "on time" text in light mode (4.2:1) below the 4.5:1 minimum, chart bars with a
label but no role, and two landmark issues; all fixed. axe can't see CSS filters, so the
offline board is marked with a dashed frame rather than dimmed (dimming took its text to
3:1).

## Fonts and other sites

The page needs no internet connection: the fonts (Atkinson Hyperlegible and Doto, both
under the SIL Open Font License, in `src/sitt/web/static/fonts/`) are bundled, and nothing
is loaded from other sites.

## The API

The page is plain HTML, CSS and JavaScript over a small JSON API, which you can also use
yourself. Interactive docs are at <http://127.0.0.1:8000/api/docs>.

| Endpoint | What it does |
| --- | --- |
| `GET /api/status` | What is loaded: trains (and `lines`: trains per line), stations, real observations, when data was last collected |
| `GET /api/stations` | Every station in the timetable: the main line from the CSMT end, then each other line's own stations in its order, each with its `line` |
| `GET /api/recommend?from=KYN&to=CSMT` | The recommendation and the next trains. Optional `at=08:00`, `date=2026-10-14`, `train=08:12`, `n=8` |
| `POST /api/collect` | One live collection round (NTES; Mobond only if both its switches are on) |
| `GET /api/patterns` | Delay by hour, part of the day and direction, from real readings (the **Delay patterns** page) |
| `GET /api/reports` | The latest crowd reports |
| `POST /api/reports` | Log crowding: `{"station": "KYN", "departure": "08:12", "service": "fast", "crowd": 4}` |
| `DELETE /api/reports/{id}` | Take back a report logged from the page in the last 15 minutes (`can_undo` in the list); 409 for older ones and the bot's |

Errors come back as `{"error": "..."}` with a 4xx status, worded for a person.

## How it fits with the rest

It reads the same database as the bot, `sitt-next` and the dashboard (`SITT_DB_PATH`, default
`data/sitt.duckdb`). DuckDB lets only one process write to the file at a time, so:

- requests inside the app take turns (one connection at a time), so several people using
  the page at once, say you and your phone, all get answers;
- while another program has the file (the bot answering a command, `sitt-collect`
  loading), the app waits a few seconds and retries. If it is still busy, the page says
  "The database is busy … Try again in a moment." The bot only holds the file for a moment
  per command, so running both is fine in practice.

Live batches from **Check live first** go beside the database (`data/` by default).

The Streamlit dashboard (`sitt-dashboard`) is still there for exploring the data: delay
patterns, model metrics, the timetable. The web app is for using it.

Code: [`src/sitt/web/app.py`](../src/sitt/web/app.py) (API),
[`src/sitt/web/static/`](../src/sitt/web/static/) (page), tests in `tests/web/`.
