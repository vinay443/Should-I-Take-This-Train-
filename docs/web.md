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
  morning).
- **The board** at the top shows the recommended train the way a platform indicator would:
  time, destination, fast or slow, coaches, and whether it is late. Under it, the one-line
  reason and what the times are based on (timetable, past delays, live readings).
- **The next trains** list each train's predicted arrival and delay, a five-step crowding
  meter, and tags (AC, 15-car, ladies' special). Tap a train for the details, or to log
  crowding for it.
- **Why this train?** shows the full explanation, the same as the bot's `/why`.
- **How crowded was your train?** saves a crowd report. It is matched to a scheduled train
  and adjusts the crowding estimate, the same as the bot's `/log`.
- **Save this route** keeps your usual trips as one-tap buttons (stored in this browser).

## Delay patterns

**Delay patterns** (`/patterns`, linked at the top of the page) shows how late trains have
really been: the median delay and the share of trains 5+ minutes late by part of the day
(early morning, the two peaks, midday, night) and hour by hour, for each direction, plus
fast against slow. Hover or tab to a bar for its numbers.

Each train counts once per station and day, with the delay of its **last** reading, and
only if that reading was taken within 15 minutes of the train's time: the same rule as
[`live-drift.md`](live-drift.md), because earlier readings often say "on time" for trains
that end up late. Only real readings of suburban trains are used, never synthetic data.
Hours or parts of the day resting on fewer than 5 trains are drawn pale and marked "too few
to go by", and a note at the top says how many trains, days and stations it all rests on.
With no real readings yet the page says so and how to collect some.

It describes the past; the recommender does the predicting. Code: `src/sitt/patterns.py`.
The Streamlit dashboard's delay heatmaps count every reading instead, so a train seen four
times weighs four times as much there.

## On your phone

Start the app with `uv run sitt-web --host 0.0.0.0`, then on your phone (same Wi-Fi) open
`http://<your PC's address>:8000` (`ipconfig` on Windows shows the address). Chrome's menu
has **Add to Home screen** (**Install app** where it is offered), and Safari's share sheet
has **Add to Home Screen**. It opens full screen with its own icon.

**When the PC can't be reached** (it is asleep, `sitt-web` isn't running, or you've left the
Wi-Fi), the page shows the last answer it got for that trip, marked **Not live** with the
time it was saved, and the header says it is offline. The phone keeps the last 8 answers.
A trip with no saved answer says so. Logging crowding needs the PC; the page tells you the
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

The manifest is at `/manifest.webmanifest`, the service worker at `/sw.js` (it keeps only the
page, its fonts, icons, the station list and the status line; answers are kept by the page
in the browser's storage). Icons are drawn by `scripts/make_web_icons.py`.

To check all of this in a real browser:

```bash
python3 scripts/check_web_offline.py          # needs Playwright + Chromium for that python3
```

It builds a throwaway database from the sample timetable, starts `sitt-web`, checks that
Chrome finds the app installable and that the worker controls the page, stops the server,
reloads, and checks the "Not live" fallback, at phone and desktop width in light and dark,
saving screenshots to a temp folder. Checked 2026-10-16: all checks pass.

## Fonts and other sites

The page needs no internet connection: the fonts (Atkinson Hyperlegible and Doto, both
under the SIL Open Font License, in `src/sitt/web/static/fonts/`) are bundled, and nothing
is loaded from other sites.

## The API

The page is plain HTML, CSS and JavaScript over a small JSON API, which you can also use
yourself. Interactive docs are at <http://127.0.0.1:8000/api/docs>.

| Endpoint | What it does |
| --- | --- |
| `GET /api/status` | What is loaded: trains, stations, real observations, when data was last collected |
| `GET /api/stations` | Every station in the timetable, in line order |
| `GET /api/recommend?from=KYN&to=CSMT` | The recommendation and the next trains. Optional `at=08:00`, `date=2026-10-14`, `train=08:12`, `n=8` |
| `POST /api/collect` | One live collection round (NTES; Mobond only if both its switches are on) |
| `GET /api/patterns` | Delay by hour, part of the day and direction, from real readings (the **Delay patterns** page) |
| `GET /api/reports` | The latest crowd reports |
| `POST /api/reports` | Log crowding: `{"station": "KYN", "departure": "08:12", "service": "fast", "crowd": 4}` |

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
