# Should I Take This Train?

Helps you decide whether to board the Mumbai local in front of you or wait for the next one.
The first target is the Central line around Kalyan.

Ask the Telegram bot `/next KYN CSMT` and it answers along the lines of:

> Take the 08:12 fast: arrives 08:58 ±4 min, likely packed, but the next best (08:27 fast)
> arrives 15 min later.

## What is real and what is synthetic

Read this before trusting any number the project shows you.

| Part | Status |
| --- | --- |
| **Timetable** | **Real.** Converted from Central Railway's official timetable PDFs: 895 main-line trains, with AC, 15-car and ladies' special markers. The base edition is from October 2024, with the 2025 AC and 2026 15-car supplements applied. |
| **Live observations** | **Real, and very few.** Collected from NTES every 15 minutes on one PC since 5 October 2026. NTES reports locals beyond Kalyan and long-distance trains, not CSMT–Kalyan locals. m-Indicator (Mobond) is switched off: no permission yet. |
| **Delay data used for development** | **Synthetic.** Invented by a generator from guesses about how delays behave. |
| **Delay model and its accuracy figures** | **Trained and tested on that synthetic data.** They show the pipeline works. They say nothing about real trains, and such a model is not used for recommendations. A real model is trained only once there is enough real data (`sitt-retrain`), and used only at the stations it was trained on. |
| **Crowding** | **A rule-of-thumb estimate**, not a measurement and not a trained model. It is adjusted by your own crowd reports as you log them. |
| **Crowd reports** | **Real**, whatever you log through the bot. Each is matched to a scheduled train where that can be done with confidence. |
| **Megablocks** | **Real but partial.** Only the date and line can be fetched; the section and times are entered by hand. |
| **`/next` today** | **Timetable times**, or the typical past delay where a few real readings exist, and the crowding estimate. Every reply says which. |

Everything synthetic is labelled where it appears: `source = 'synthetic'` in the data, a
banner in the dashboard, and a line in any bot reply that uses it. Once real observations
have been collected, the same commands run on them with no code changes.

## Setup

Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 for you if needed.

```bash
uv sync
```

```bash
cp .env.example .env
```

```bash
uv run sitt-init-db
```

This creates the DuckDB database at `data/sitt.duckdb`, or wherever `SITT_DB_PATH` points.
The schema, with a comment on each table, is in
[`src/sitt/db/schema.sql`](src/sitt/db/schema.sql). Applying it again to an existing database
is safe.

## Load the timetable

Download four PDFs from Central Railway. Their URLs are at the top of
[`src/sitt/ingest/cr_pdf.py`](src/sitt/ingest/cr_pdf.py): the main-line DOWN and UP
timetables, and the AC and 15-car supplements. Then convert them to the project's CSV format
([`docs/timetable-format.md`](docs/timetable-format.md)):

```bash
uv run python -m sitt.ingest.cr_pdf dn.pdf up.pdf --ac-supplement ac.pdf --15-car-supplement cars.pdf -o central.csv
```

The converter prints one line for every train a supplement changed, added or couldn't be
applied to. Load the result, replacing any earlier timetable for the line:

```bash
uv run python -m sitt.ingest.timetable central.csv --replace
```

To try things without the PDFs, load the small invented sample instead:

```bash
uv run python -m sitt.ingest.timetable tests/fixtures/sample_timetable.csv
```

## Run the bot

Set up a Telegram bot token and your user ID as described in
[`docs/bot-setup.md`](docs/bot-setup.md), then:

```bash
uv run sitt-bot
```

| Command | What it does |
| --- | --- |
| `/next KYN CSMT` | Recommends one of the next trains, and lists each with its predicted arrival, estimated crowding and tags |
| `/why` | Explains the last recommendation |
| `/log 8:12 fast KYN packed` | Logs how crowded your train was, and says which scheduled train it matched. `/log` on its own asks step by step |
| `/mylogs` | Your last 10 reports |
| `/fav add work KYN CSMT 8:12` | Saves a route and your usual train |
| `/commute` | `/next` for your saved route; the return leg in the afternoon |

With a usual train saved, the bot also sends the recommendation before it leaves and asks,
with one tap, how crowded it was after it arrives. Both only happen while the bot is running.

How the recommendation is made, and its thresholds, are in
[`docs/recommender.md`](docs/recommender.md). The crowding rules are in
[`docs/crowding.md`](docs/crowding.md).

## Collect live data

The collector takes a snapshot of running status from NTES, and from m-Indicator (Mobond)
if that is ever switched on, and stores it as observations. See
[`docs/collector.md`](docs/collector.md).

- **m-Indicator is switched off everywhere by default.** It is a private endpoint of a
  commercial app. It needs two separate settings before a single request is made, and should
  stay off until Mobond has agreed. The checklist is in
  [`docs/data-sources.md`](docs/data-sources.md#turning-mobond-on-a-checklist-for-when-permission-arrives).
- **On GitHub:** the workflows exist but only start by hand. Run **Probe live sources** first
  to check the sources answer GitHub's servers.
- **On this machine:** `uv run sitt-collect` does one round and appends to the local database.
  [`docs/collector.md`](docs/collector.md#6-running-it-on-this-windows-machine) shows how to
  run it every 15 minutes with Windows Task Scheduler.

Keeping an eye on it, and on the data:

| Command | What it does |
| --- | --- |
| `uv run sitt-health` | Runs expected and recorded, gaps, source failures, matched trains, data freshness. `--telegram` sends a daily summary; `--alert-only` sends a message only when something is wrong ([`collector.md`](docs/collector.md#8-the-health-report)) |
| `uv run sitt-dq` | Flags duplicates, implausible delays and timestamp mistakes; lists trains and stations the timetable doesn't know. Deletes nothing ([`data-quality.md`](docs/data-quality.md)) |
| `uv run sitt-match-logs` | Matches older crowd reports to scheduled trains ([`crowding.md`](docs/crowding.md#matching-a-report-to-a-train)) |
| `uv run sitt-retrain` | Trains on real data once there is enough, and promotes the model only if it beats the baselines. Until then, says how far along the data is ([`retraining.md`](docs/retraining.md)) |
| `uv run sitt-export export` | Writes the observations as monthly Parquet files, never including a source's own text. `compact` merges the collector's small files ([`collector.md`](docs/collector.md#9-export-and-compaction)) |

Megablocks (planned maintenance) are a separate, small job:
[`docs/megablocks.md`](docs/megablocks.md).

```bash
uv run sitt-block fetch
```

## Generate synthetic data and train the model

Real observations are still far too few to train on, so development uses invented ones for
the loaded timetable. They go into a separate database, never the real one. The assumptions are in
[`docs/synthetic-data.md`](docs/synthetic-data.md).

```bash
uv run python -m sitt.synth --weeks 16
```

Train the delay model on them, evaluate it against the baselines, and save it to `models/`:

```bash
uv run python -m sitt.models train
```

Write the results page:

```bash
uv run python -m sitt.models report --write docs/model-results.md
```

The current results are in [`docs/model-results.md`](docs/model-results.md), headed as
synthetic.

**A model trained on synthetic data is not used for recommendations.** `/next` and the
dashboard ignore it and give timetable times, so real trains never get predictions learned
from invented delays. To try such a model anyway, for testing, set
`SITT_ALLOW_SYNTHETIC_MODEL=true` in `.env`; every reply then says the predictions are
synthetic.

Training on real observations is `sitt-retrain`'s job. It checks there is enough data, leaves
out suspect readings, and promotes a model only if it beats the baselines:

```bash
uv run sitt-retrain
```

## Dashboard

```bash
uv run sitt-dashboard
```

Five pages: a timetable explorer, delay patterns, crowd reports, an interactive recommender
and model metrics. It can also be deployed to Streamlit Community Cloud without secrets. See
[`docs/dashboard.md`](docs/dashboard.md).

## Development

```bash
uv run ruff check
```

```bash
uv run ruff format
```

```bash
uv run pytest
```

CI runs the same checks on every push and pull request. Tests can't touch the network: any
attempt to connect beyond this machine fails the test that made it.

After changing dependencies, regenerate the file Streamlit Community Cloud installs from:

```bash
uv export --no-dev --no-hashes --no-emit-project --format requirements-txt -o requirements.txt
```

## Layout

```
src/sitt/
  config.py       settings from environment variables / .env
  db/             DuckDB schema and connection helpers
  timetable.py    queries over the timetable (next trains between stations)
  routes.py       each train's scheduled position at every station, including ones it passes
  holidays.py     days that run to the Sunday timetable (movable dates in holidays.toml)
  ingest/
    cr_pdf.py     Central Railway timetable PDFs -> timetable CSV
    overrides.py  manual corrections to the timetable (timetable_overrides.toml)
    timetable.py  timetable CSV -> database
    live/         live collector (NTES, m-Indicator), loader, local runner, run log
    blocks.py     megablocks: fetch, parse, enter by hand
  health.py       collector health report, Telegram summary and alerts
  dq.py           data-quality checks on observations
  matching.py     which scheduled train a crowd report is about
  export.py       Parquet export and compaction
  notify.py       sending a Telegram message without the bot running
  synth/          synthetic observation generator
  models/         delay features, baselines, LightGBM model, retraining, crowding estimate
  recommend.py    "take this train or wait?"
  bot/            Telegram bot: logging, /next, saved routes, /commute, its own messages
  dashboard/      Streamlit app
scripts/          PowerShell scripts for Task Scheduler: collector, health checks, retraining
streamlit_app.py  entry point for Streamlit Community Cloud
tests/
docs/
data/             local databases and collected files (gitignored)
models/           trained models (gitignored)
```

## Documentation

| Doc | About |
| --- | --- |
| [`data-sources.md`](docs/data-sources.md) | What timetable, live and crowding data exists, what was tried, and the checklist for turning Mobond on |
| [`timetable-format.md`](docs/timetable-format.md) | The timetable CSV format and loader, manual overrides, station codes and holidays |
| [`collector.md`](docs/collector.md) | The live collector: local runs, the run log, health report and alerts, export, GitHub workflows |
| [`data-quality.md`](docs/data-quality.md) | The checks on observations and what gets flagged |
| [`retraining.md`](docs/retraining.md) | Training on real data: readiness gates, promotion, the model registry |
| [`long-distance-feature.md`](docs/long-distance-feature.md) | An experimental model input, switched off |
| [`expansion-assessment.md`](docs/expansion-assessment.md) | What adding the Harbour and Western lines would take |
| [`megablocks.md`](docs/megablocks.md) | Megablock announcements and manual entry |
| [`synthetic-data.md`](docs/synthetic-data.md) | The synthetic generator and its assumptions |
| [`model-results.md`](docs/model-results.md) | Delay model results (synthetic), with one section on real data so far |
| [`crowding.md`](docs/crowding.md) | The crowding rules, and how reports are matched to trains |
| [`recommender.md`](docs/recommender.md) | The decision rule and thresholds |
| [`bot-setup.md`](docs/bot-setup.md) | Setting up and using the Telegram bot, saved routes and its own messages |
| [`dashboard.md`](docs/dashboard.md) | Running and deploying the dashboard |

## Roadmap

Done:

1. **Data source research.** What exists and how reliable it is.
2. **Timetable.** The official PDFs, with supplements, converted and loaded; manual overrides
   and the movable holidays for 2026.
3. **Live collector**, running on one PC against NTES since 5 October 2026, with a run log,
   a health report and Telegram alerts.
4. **Data-quality checks** on what it collects.
5. **Crowd logging bot**, with reports matched to scheduled trains, saved routes, `/commute`,
   a morning message and a one-tap crowd prompt.
6. **Synthetic data, baselines and a LightGBM delay model**, evaluated by time.
7. **Crowding estimate** from rules and your own reports.
8. **Megablock table**, with fetching of dates and manual entry of details.
9. **Recommender**, in the bot (`/next`, `/why`) and the dashboard.
10. **Dashboard.**
11. **A pipeline for real-data models** that waits until there is enough data and promotes a
    model only if it beats the baselines.
12. **Parquet export and compaction.**

Next, roughly in order:

1. **Let real data accumulate**, and watch it with `sitt-health` and `sitt-dq`. Review the
   data-quality bounds and alert thresholds after a few weeks.
2. **Ask Mobond for permission.** NTES doesn't cover CSMT–Kalyan locals, which is the core of
   this project. The adapter is ready and off.
3. **Log crowding**, ideally with a saved route so the bot asks after each commute. Tune the
   crowding rules once there are enough reports.
4. **Read the first real model results sceptically** when `sitt-retrain` produces them, and
   check the synthetic assumptions against reality.
5. **Add the 2027 holidays** when Maharashtra publishes them (the entries are there, marked
   tentative).
6. **Use live readings in recommendations**: cancellations and the train ahead.
7. **Refresh the timetable** when Central Railway publishes a new main-line edition.
8. **Other lines** only after that: see [`expansion-assessment.md`](docs/expansion-assessment.md).
