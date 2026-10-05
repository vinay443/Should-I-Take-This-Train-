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

That sample is invented. For the real Central main line timetable, download the PDFs linked
at the top of [`src/sitt/ingest/cr_pdf.py`](src/sitt/ingest/cr_pdf.py): the two main PDFs
(DOWN and UP) and, optionally, the AC and 15-car supplements that update them. Convert them to
that CSV format:

```bash
uv run python -m sitt.ingest.cr_pdf dn.pdf up.pdf --ac-supplement ac.pdf --15-car-supplement cars.pdf -o central.csv
```

The converter prints one line for every train a supplement changed, added or couldn't be
applied to. Then load the result, replacing the line's earlier timetable:

```bash
uv run python -m sitt.ingest.timetable central.csv --replace
```

To run the Telegram crowd-logging bot, see [`docs/bot-setup.md`](docs/bot-setup.md).

To collect live running status from m-Indicator and NTES, see
[`docs/collector.md`](docs/collector.md). The collector is built, but its 15-minute schedule
is not switched on yet.

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

1. **Data source research** (done, see [`docs/data-sources.md`](docs/data-sources.md)):
   find what timetable and live running data exists for Mumbai locals and how reliable it is.
2. **Timetable ingestion** (done, see [`docs/timetable-format.md`](docs/timetable-format.md)):
   load stations, trains and scheduled stops for the Central line. A parser for Central
   Railway's timetable PDFs, including the AC and 15-car supplements, is built
   (`sitt.ingest.cr_pdf`).
3. **Live collector** (built, schedule not yet switched on, see
   [`docs/collector.md`](docs/collector.md)): poll live running status on a schedule and store
   observations.
4. **Crowd logging bot** (done, see [`docs/bot-setup.md`](docs/bot-setup.md)): a Telegram
   bot that lets riders report how crowded their train is.
5. **Baseline models**: simple benchmarks such as historical averages by train, station and
   time of day.
6. **LightGBM model**: a gradient-boosted model for delay and crowding.
7. **Recommendations**: answer "should I take this train or wait for the next one?"
