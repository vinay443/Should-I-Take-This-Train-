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
| **Timetable** | **Real.** Converted from Central Railway's official timetable PDFs: 894 main-line trains, with AC, 15-car and ladies' special markers. The base edition is from October 2024, with the 2025 AC and 2026 15-car supplements applied. |
| **Live observations** | **None collected yet.** The collector is built and tested, but its schedule is not switched on. |
| **Delay data used for development** | **Synthetic.** Invented by a generator from guesses about how delays behave. |
| **Delay model and its accuracy figures** | **Trained and tested on that synthetic data.** They show the pipeline works. They say nothing about real trains, and such a model is not used for recommendations. |
| **Crowding** | **A rule-of-thumb estimate**, not a measurement and not a trained model. It is adjusted by your own crowd reports as you log them. |
| **Crowd reports** | **Real**, whatever you log through the bot. |
| **Megablocks** | **Real but partial.** Only the date and line can be fetched; the section and times are entered by hand. |
| **`/next` today** | **Timetable times** and the crowding estimate. No delay predictions until real observations exist. |

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
| `/log 8:12 fast KYN packed` | Logs how crowded your train was. `/log` on its own asks step by step |
| `/mylogs` | Your last 10 reports |

How the recommendation is made, and its thresholds, are in
[`docs/recommender.md`](docs/recommender.md). The crowding rules are in
[`docs/crowding.md`](docs/crowding.md).

## Collect live data

The collector takes a snapshot of running status from m-Indicator (Mobond) and NTES and stores
it as observations. See [`docs/collector.md`](docs/collector.md).

- **m-Indicator is switched off everywhere by default.** It is a private endpoint of a
  commercial app. Don't enable it until Mobond has agreed.
- **On GitHub:** the workflows exist but only start by hand. Run **Probe live sources** first
  to check the sources answer GitHub's servers.
- **On this machine:** `uv run sitt-collect` does one round and appends to the local database.
  [`docs/collector.md`](docs/collector.md#6-running-it-on-this-windows-machine) shows how to
  run it every 15 minutes with Windows Task Scheduler.

Megablocks (planned maintenance) are a separate, small job:
[`docs/megablocks.md`](docs/megablocks.md).

```bash
uv run sitt-block fetch
```

## Generate synthetic data and train the model

Until real observations exist, generate invented ones for the loaded timetable. They go into
a separate database, never the real one. The assumptions are in
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

To train on real observations later, pass the real database:

```bash
uv run python -m sitt.models train --db data/sitt.duckdb
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

CI runs the same checks on every push and pull request. Tests never touch the network.

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
  holidays.py     days that run to the Sunday timetable
  ingest/
    cr_pdf.py     Central Railway timetable PDFs -> timetable CSV
    timetable.py  timetable CSV -> database
    live/         live collector (m-Indicator, NTES), loader, local runner
    blocks.py     megablocks: fetch, parse, enter by hand
  synth/          synthetic observation generator
  models/         delay features, baselines, LightGBM model, crowding estimate
  recommend.py    "take this train or wait?"
  bot/            Telegram bot
  dashboard/      Streamlit app
scripts/          PowerShell scripts for running the collector with Task Scheduler
streamlit_app.py  entry point for Streamlit Community Cloud
tests/
docs/
data/             local databases and collected files (gitignored)
models/           trained models (gitignored)
```

## Documentation

| Doc | About |
| --- | --- |
| [`data-sources.md`](docs/data-sources.md) | What timetable, live and crowding data exists, and what was tried |
| [`timetable-format.md`](docs/timetable-format.md) | The timetable CSV format and loader |
| [`collector.md`](docs/collector.md) | The live collector: GitHub workflows, local runs, what is published |
| [`megablocks.md`](docs/megablocks.md) | Megablock announcements and manual entry |
| [`synthetic-data.md`](docs/synthetic-data.md) | The synthetic generator and its assumptions |
| [`model-results.md`](docs/model-results.md) | Delay model results (synthetic) |
| [`crowding.md`](docs/crowding.md) | The crowding rules |
| [`recommender.md`](docs/recommender.md) | The decision rule and thresholds |
| [`bot-setup.md`](docs/bot-setup.md) | Setting up and using the Telegram bot |
| [`dashboard.md`](docs/dashboard.md) | Running and deploying the dashboard |

## Roadmap

Done:

1. **Data source research.** What exists and how reliable it is.
2. **Timetable.** The official PDFs, with supplements, converted and loaded.
3. **Live collector.** Built for GitHub and for this machine. Not yet switched on.
4. **Crowd logging bot.**
5. **Synthetic data, baselines and a LightGBM delay model**, evaluated by time.
6. **Crowding estimate** from rules and your own reports.
7. **Megablock table**, with fetching of dates and manual entry of details.
8. **Recommender**, in the bot (`/next`, `/why`) and the dashboard.
9. **Dashboard.**

Next, roughly in order:

1. **Get real data flowing.** Run the probe from GitHub. Ask Mobond for permission. Switch on
   a collector, on GitHub or on this machine.
2. **Retrain on real observations** after a few weeks, and replace the synthetic results page.
   Expect the features and model settings to need rework once real delays are visible.
3. **Check the synthetic assumptions against reality**, and retire the generator from
   everything except tests.
4. **Add the movable holidays** to `sitt/holidays.py` each year.
5. **Tune the crowding rules** against logged reports, once there are enough of them.
6. **Use live readings in recommendations**: cancellations and the train ahead only matter
   once the collector runs.
7. **Refresh the timetable** when Central Railway publishes a new main-line edition.
