# Should I Take This Train?

Forecasts delays and crowding on Mumbai local trains, so you can decide whether to board the
train in front of you or wait for the next one.

The first target is the Central line around Kalyan. The plan is to collect timetable and live
running data, add crowd reports from riders through a Telegram bot, and train models that
predict how late and how full a given train will be.

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
[`src/sitt/db/schema.sql`](src/sitt/db/schema.sql).

Load a timetable CSV, in the format described in
[`docs/timetable-format.md`](docs/timetable-format.md):

```bash
uv run python -m sitt.ingest.timetable tests/fixtures/sample_timetable.csv
```

To run the Telegram crowd-logging bot, see [`docs/bot-setup.md`](docs/bot-setup.md).

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

CI runs the same checks on every push and pull request.

## Layout

```
src/sitt/
  config.py   settings from environment variables / .env
  db/         DuckDB schema and connection helpers
  ingest/     data collection (timetables, live running status)
  timetable.py  queries over the static timetable (next trains between stations)
  models/     delay and crowding forecasts
  bot/        Telegram bot for crowd reports and recommendations
tests/
data/         local database (gitignored)
```

## Roadmap

1. **Data source research**: find what timetable and live running data exists for Mumbai
   locals and how reliable it is.
2. **Timetable ingestion**: load stations, trains and scheduled stops for the Central line.
3. **Live collector**: poll live running status on a schedule and store observations.
4. **Crowd logging bot**: a Telegram bot that lets riders report how crowded their train is.
5. **Baseline models**: simple benchmarks such as historical averages by train, station and
   time of day.
6. **LightGBM model**: a gradient-boosted model for delay and crowding.
7. **Recommendations**: answer "should I take this train or wait for the next one?"
