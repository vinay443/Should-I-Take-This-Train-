# Timetable CSV format

The static timetable enters the database through one normalised CSV format, loaded by
[`sitt.ingest.timetable`](../src/sitt/ingest/timetable.py). Whatever the real data source
turns out to be, supporting it means writing a converter that produces this CSV. Nothing
else should write to `stations`, `trains` or `scheduled_stops`.

There is **one row per train per stop**. A sample covering a few Kalyan–CSMT trains is in
[`tests/fixtures/sample_timetable.csv`](../tests/fixtures/sample_timetable.csv). Its
stations are real, but its train numbers and timings are invented.

```csv
train_number,destination,service_type,direction,station_code,station_name,scheduled_arrival,scheduled_departure,days
90106,CSMT,fast,up,KYN,Kalyan,,07:12,daily
90106,CSMT,fast,up,DI,Dombivli,07:18,07:19,daily
90106,CSMT,fast,up,TNA,Thane,07:33,07:34,daily
...
90106,CSMT,fast,up,CSMT,Chhatrapati Shivaji Maharaj Terminus,08:19,,daily
```

## Columns

Column names are case-insensitive and may be in any order. Columns not listed here are
ignored, so converters can keep other source fields, such as platform numbers, alongside
them.

| Column | Required | Meaning | Stored as |
|---|---|---|---|
| `train_number` | yes | Public train number, e.g. `96301`. | `trains.number` |
| `destination` | yes | Destination text as riders see it on the board, e.g. `Kalyan`, `CSMT`. | `trains.label` |
| `service_type` | yes | `fast` or `slow`. | `trains.train_type` |
| `direction` | yes | `up` (towards CSMT) or `down` (away from CSMT). | `trains.direction` |
| `station_code` | yes | Railway station code, e.g. `KYN`. Upper-cased on load. | `stations.code`, `scheduled_stops.station_code` |
| `station_name` | yes | Station name, e.g. `Kalyan`. | `stations.name` |
| `scheduled_arrival` | see below | `HH:MM` or `HH:MM:SS`, 24-hour clock, Mumbai time. | `scheduled_stops.scheduled_arrival` |
| `scheduled_departure` | see below | Same format. | `scheduled_stops.scheduled_departure` |
| `days` | yes (may be blank) | Days the train runs; see [Running days](#running-days). | `scheduled_stops.days_of_operation` |
| `train_id` | no | Stable internal ID; defaults to `<line>-<train_number>`, e.g. `central-96301`. | `trains.train_id` |
| `service_code` | no | The timetable's own code for the service, e.g. `A 1`. | `trains.service_code` |
| `ac` | no | `yes` or `no`: an air-conditioned rake. | `trains.is_ac` |
| `cars` | no | Rake length in cars, e.g. `15`. | `trains.car_count` |
| `notes` | no | Flags joined by `\|`. `ladies_special` marks a train reserved for women; other flags, such as the PDF converter's `non_ac_weekends`, are ignored. | `trains.is_ladies_special` |

Every value is trimmed of surrounding whitespace.

## Rules

**Row order is route order.** A train's rows must be listed in the order the train
calls at the stations; that order becomes `stop_seq` (1, 2, 3, …). One train's rows don't
need to be next to each other in the file, but keeping them together makes the file
easier to read.

**Times.**
- The first stop needs a departure, and the last stop needs an arrival.
- Every stop in between needs at least one of the two. When a source gives only one time,
  put it in whichever column the source means, or in both.
- Leave the arrival blank at the station where the train starts. When the file covers
  only part of a longer route (e.g. a Kasara train cut off at Kalyan), fill in both times
  at the edge stations.
- Times must increase along the route. They wrap past midnight with no special marker:
  `23:59` at Byculla followed by `00:02` at Chinchpokli is fine.
- Consecutive times more than 2 hours apart are rejected as out of order, and so is a
  whole run of 12 hours or more.

**Train attributes are per train.** `train_number`, `destination`, `service_type` and
`direction` must be the same on every row of a train, and so must the optional
`service_code`, `ac`, `cars` and `notes`.

**Optional attributes may be unknown.** A file without one of the optional columns leaves
that attribute NULL in the database, and so does a blank `service_code`, `ac` or `cars`
value: NULL means the source didn't say. `notes` is different: when the column is present,
a train without the `ladies_special` flag is stored as not a ladies' special. Loading a
train again replaces its attributes along with everything else, so reloading from a file
without these columns clears them.

**Stations.**
- A station can appear only once per train.
- A station code must have the same `station_name` everywhere in the file.
- All trains must agree on the order of stations along the line, with up trains read in
  reverse. A train labelled with the wrong direction shows up as a conflict. The loader
  derives `stations.seq` from this order, numbering from the CSMT end.

**One file per line.** A file is loaded for one line (`--line`, default `central`).
`stations.line` and `trains.line` are set from it.

**Comments.** Lines that start with `#` and blank lines are ignored. Use them for
provenance, e.g. `# source: …, fetched 2026-10-01`. Error messages still use the real
line numbers.

**Encoding.** UTF-8. A byte-order mark, as Excel writes, is fine.

### Running days

`days` gives the days a train runs, counted from the day it **starts its run**. A train
that leaves CSMT at 23:52 on Sundays only is a Sunday train at every stop, including the
stops it reaches after midnight. Accepted forms:

- blank or `daily`: every day
- a Monday-to-Sunday `Y`/`N` mask, e.g. `YYYYYYN` (the form the database stores)
- day names (`mon` … `sun`) and ranges joined by `|`, e.g. `mon-sat`, `sun`,
  `sat|sun`, `mon|wed-fri`. Ranges may wrap around the week, as in `fri-mon`.

`days` is stored per stop, so a train can, for example, skip a station on Sundays.

### Train IDs

By default, `train_id` is `<line>-<train_number>`, so a train number must identify one
service within a file. If a source reuses a number, for example for different weekday and
Sunday services, the converter must supply an explicit `train_id` (such as `96301-sun`)
so the services stay apart. `train_number` can then repeat.

## Loading

```bash
uv run python -m sitt.ingest.timetable path/to/timetable.csv
```

Options:

- `--line NAME`: the line the file is for (default `central`).
- `--replace`: treat the file as the complete timetable for that line, and delete that
  line's trains that aren't in it. Without this flag, trains missing from the file are
  kept.
- `--db PATH`: the database to load into (default `SITT_DB_PATH`, or
  `data/sitt.duckdb`).

The whole file is validated before the database is touched. Any problem aborts the load
and prints every error with its line number.

**Re-running is safe.** A load runs in one transaction, and each train in the file
replaces the stored train with the same `train_id` together with all of its stops.
Loading the same file twice leaves the database exactly as one load does. After each
load, `stations.seq` is recomputed from every train on the line. If the new trains
conflict with the station order of trains already loaded, the load is rolled back.

Limitations, inherited from the schema:

- A station belongs to one line. Loading a second line that shares a station, such as the
  Harbour line at CSMT, moves that station to the line loaded last.
- Stations are never deleted, even when no train calls there any more.

From Python:

```python
from sitt.db import init_db
from sitt.ingest.timetable import load_timetable, read_timetable

timetable = read_timetable("timetable.csv", line="central")  # raises TimetableError
with init_db() as con:
    load_timetable(con, timetable, replace=True)
```

## Querying

`sitt.timetable.next_trains` returns the next trains between two stations:

```python
from datetime import datetime
from sitt.timetable import next_trains

for trip in next_trains(con, "Kalyan", "CSMT", datetime.now(), n=5):
    print(trip.number, trip.train_type, trip.departure, trip.arrival)
```

Each trip also carries `service_code`, `is_ac`, `car_count` and `is_ladies_special`, which
are `None` when the timetable didn't give them.

Stations can be given by code or name, in any case. Naive datetimes are treated as
Mumbai time, and aware ones are converted to IST. Running days and trains crossing
midnight are handled.

## Writing a converter

A converter for a new source should:

1. Map the source's stations to railway codes and names.
2. Emit one row per train per stop, in route order.
3. Normalise `service_type`, `direction` and `days` to the values above.
4. Write a `#` header comment recording the source and when it was fetched.

Then run the loader on the output. Its validation reports any rows the converter got
wrong.
