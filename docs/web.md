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
fetched only when you press **Check live first**, exactly like `sitt-next --collect`.

## What the page does

- **Pick a trip.** From and To accept a name or a code (`Kalyan`, `KYN`); mistyped stations
  get "Did you mean…?". **Another time** asks about a later time or another day.
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

The page works offline: the fonts (Atkinson Hyperlegible and Doto, both under the SIL Open
Font License, in `src/sitt/web/static/fonts/`) are bundled, and nothing is loaded from
other sites.

## The API

The page is plain HTML, CSS and JavaScript over a small JSON API, which you can also use
yourself. Interactive docs are at <http://127.0.0.1:8000/api/docs>.

| Endpoint | What it does |
| --- | --- |
| `GET /api/status` | What is loaded: trains, stations, real observations, when data was last collected |
| `GET /api/stations` | Every station in the timetable, in line order |
| `GET /api/recommend?from=KYN&to=CSMT` | The recommendation and the next trains. Optional `at=08:00`, `date=2026-10-14`, `train=08:12`, `n=8` |
| `POST /api/collect` | One live collection round (NTES; Mobond only if both its switches are on) |
| `GET /api/reports` | The latest crowd reports |
| `POST /api/reports` | Log crowding: `{"station": "KYN", "departure": "08:12", "service": "fast", "crowd": 4}` |

Errors come back as `{"error": "..."}` with a 4xx status, worded for a person.

## How it fits with the rest

It reads the same database as the bot, `sitt-next` and the dashboard (`SITT_DB_PATH`, default
`data/sitt.duckdb`). Recommendations open it read-only, so the bot can run at the same time.
Live batches from **Check live first** go beside the database (`data/` by default).

The Streamlit dashboard (`sitt-dashboard`) is still there for exploring the data: delay
patterns, model metrics, the timetable. The web app is for using it.

Code: [`src/sitt/web/app.py`](../src/sitt/web/app.py) (API),
[`src/sitt/web/static/`](../src/sitt/web/static/) (page), tests in `tests/web/`.
