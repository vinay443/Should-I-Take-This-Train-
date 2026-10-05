# Synthetic data

**Everything this generator produces is invented.** No real observations have been collected
yet, so `sitt.synth` makes up delays for the real timetable. That lets the feature pipeline,
models, recommender and dashboard be built and tested now. The patterns below are **guesses
about how Mumbai locals might behave, not findings**. Nothing learned from this data says
anything about real trains.

## Generating it

Load the real timetable first (see the [README](../README.md#setup)), then:

```bash
uv run python -m sitt.synth --weeks 16
```

| Option           | Default                  | Meaning                                              |
| ---------------- | ------------------------ | ---------------------------------------------------- |
| `--weeks`        | `16`                     | How many weeks to invent                             |
| `--start`        | `2026-06-01`             | First day (a Monday at the start of the monsoon)     |
| `--seed`         | `1`                      | Random seed. The same options always give the same data |
| `--db`           | `data/synthetic.duckdb`  | Database to build. Replaced if it already exists     |
| `--out`          | `data/synthetic`         | Folder for the Parquet batches                       |
| `--timetable-db` | `data/sitt.duckdb`       | Where to copy the timetable from                     |
| `--long-distance [N]` | off                 | Also invent `N` long-distance trains a day at Kalyan (24 if no number is given), to exercise the [long-distance feature](long-distance-feature.md). Changes no other reading |

Sixteen weeks take about three minutes and produce roughly half a million observations.

It writes two things, both gitignored:

- `data/synthetic/observations/<UTC date>/<batch>.parquet`: one file per 15-minute poll,
  with the same columns, types and folder layout as the live collector's files.
- `data/synthetic.duckdb`: a copy of the timetable, the observations loaded with the
  collector's own loader (`sitt.ingest.live.load`), and the invented megablocks in `blocks`.

## How it is kept apart from real data

- It **never writes to `data/sitt.duckdb`**, and refuses to overwrite any database that holds
  non-synthetic observations or crowd reports.
- Every observation has `source = 'synthetic'`, and every batch ID ends in `-synth0`.
- Every invented megablock has `source = 'synthetic'`.
- The output folder carries a `SYNTHETIC-DATA.txt` marker, and the generator won't clear a
  folder that lacks it.
- Models record whether they were trained on synthetic data, and the bot and dashboard say so
  wherever such a model or such data is used.

## Replacing it with real data

Nothing downstream knows the data is synthetic except by reading the `source` column. The
feature builder, models, recommender and dashboard read the `observations`, `trains`,
`scheduled_stops`, `stations` and `blocks` tables. Once the collector has gathered real
observations and they are loaded into `data/sitt.duckdb` (see [`collector.md`](collector.md)),
point the same commands at that database instead. No code changes are needed.

## What a reading looks like

The generator copies the shape of m-Indicator's feed as the collector parses it
([`collector.md`](collector.md)): at each poll, one reading per running train, saying where it
is and how late.

- `event` is `at`, `arriving`, `crossed` or `between`, with `station_code` the station
  concerned (for `between`, the last one passed). Fast trains are reported at stations they
  pass, as in the real feed.
- `delay_minutes` is a whole number.
- About 40% of readings are marked `less_accurate` and carry up to 2 minutes of extra noise.
- A cancelled train gives `cancelled` readings, with no station, for as long as it would have
  been running.
- About 3% of polls are missing, because scheduled GitHub runs are sometimes skipped.

## The assumptions

All of these are numbers in `SynthConfig` in
[`src/sitt/synth/generate.py`](../src/sitt/synth/generate.py). Each is a guess.

| Pattern | What the generator does | Why this guess |
| --- | --- | --- |
| Delay builds along a route | A train starts about a minute late on average, then gains a little at each station, with noise. Above 3 minutes late it wins back 3% of the excess per station. | Dwell-time overruns add up; timetables have some slack. |
| Peak hours | The gain per station is higher from 08:00–11:00 towards CSMT and 17:30–21:00 away from it, with a weaker hour either side. | Crowded platforms lengthen stops. The hours are the ones this project was asked to model. |
| Good and bad days | Each day has one random factor that scales every delay. | Some days are just worse. |
| Monsoon | Days in June–September are 1.5 times worse. About 15% of them are heavy-rain days: 2.5 times worse again, with three times the cancellations. | Waterlogging is the best-known cause of disruption. The sizes are invented. |
| State of the line | Trains running the same way on the same tracks (fast or slow) share a slowly changing delay level, and each train is pulled up towards it. About one day in four has an incident that adds 8–25 minutes for 45–120 minutes. | Trains queue behind each other. This is what makes "the train ahead is late" a useful signal. |
| Long-distance traffic | About 10% of fast trains (22% at busy long-distance hours) are held 3–10 minutes somewhere between Thane and Kalyan. Slow trains never are. | Fast locals share tracks with mail and express trains. |
| Megablocks | On about three Sundays in four, one section's fast or slow lines are blocked for about five hours. Trains on those tracks gain 0.8–1.8 minutes per station in the section, and 12% of them are cancelled. | Central Railway runs maintenance blocks on most Sundays. The sections and times are typical-looking examples, not a real schedule. |
| Cancellations | 0.4% of trains on an ordinary day. | A guess. |
| Sunday timetable | Sundays and the six fixed-date holidays in `sitt.holidays` use the Sunday running days. | From Central Railway's holiday list. The movable festivals are not included. |

What the generator does **not** model: individual stations being worse than others, platform
changes, a late train's rake causing its next trip to start late, bunching after a
cancellation, festival crowds, or any link between delay and crowding. Real data will have
structure this data lacks, and may lack structure this data has.

## Why bother, if it's all made up

- The pipeline gets exercised end to end on data of the right shape and size.
- Bugs such as leaking future information into features show up as implausibly good results.
- The generator's patterns are known, so it is possible to check that a model finds them.

What it cannot do is say how accurate a model will be on real trains. See
[`model-results.md`](model-results.md).
