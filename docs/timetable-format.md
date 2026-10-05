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
| `notes` | no | Flags joined by `\|`. `ladies_special` marks a train reserved for women. `non_ac_weekends` marks an AC train that runs without AC on Saturdays and Sundays. Other flags are ignored. | `trains.is_ladies_special`, `trains.ac_weekdays_only` |

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
a train without the `ladies_special` flag is stored as not a ladies' special, and likewise
for `non_ac_weekends`. Loading a
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

Each trip also carries `service_code`, `is_ac`, `car_count`, `is_ladies_special` and
`ac_weekdays_only`, which are `None` when the timetable didn't give them. `trip.runs_ac` says
whether that particular run is air-conditioned, allowing for AC trains that run without AC
at weekends.

Stations can be given by code or name, in any case. Naive datetimes are treated as
Mumbai time, and aware ones are converted to IST. Running days and trains crossing
midnight are handled. On a holiday listed in [Holidays](#holidays) the Sunday timetable is
used, whatever day of the week it is.

## Writing a converter

A converter for a new source should:

1. Map the source's stations to railway codes and names.
2. Emit one row per train per stop, in route order.
3. Normalise `service_type`, `direction` and `days` to the values above.
4. Write a `#` header comment recording the source and when it was fetched.

Then run the loader on the output. Its validation reports any rows the converter got
wrong.

## Manual overrides

The PDF converter ([`sitt.ingest.cr_pdf`](../src/sitt/ingest/cr_pdf.py)) never guesses. A
train it can't read is left out and reported. When the PDF is wrong, or the converter reads
it wrongly, the fix goes in one committed, human-editable file:

[`src/sitt/ingest/timetable_overrides.toml`](../src/sitt/ingest/timetable_overrides.toml)

It is applied last, after the main PDFs and the AC and 15-car supplements, every time the
converter runs (including when the dashboard builds its own timetable). It ships with no
corrections in it.

### Format

The file is [TOML](https://toml.io). Each correction is one `[[train]]` table:

```toml
[[train]]
number = "96415"
reason = "PTT DN page 7 prints 14:91 at Diva; the UP page and NTES both give 14:19."
stop_times = { DIVA = "14:19" }
```

`number` (the 5-digit train number, in quotes) and `reason` are always required. The reason
should say what is wrong and how you know, because it is printed whenever the override is
applied. `action` says what kind of correction it is:

**`action = "set"`** (the default) changes a train the converter produced. Give any of:

| Key | Example | Meaning |
| --- | --- | --- |
| `days` | `"mon-sat"` | Running days, in any form the CSV accepts: `daily`, `mon-fri`, `sun`, `sat\|sun`, a mask |
| `service_type` | `"fast"` | `fast` or `slow` |
| `service_code` | `"T 15"` | The timetable's code for the service |
| `ac` | `true` | An air-conditioned train |
| `cars` | `15` | `12` or `15` |
| `notes` | `["ladies_special"]` | Replaces the train's notes. Also `non_ac_weekends` |
| `stop_times` | `{ DIVA = "14:19" }` | Changes the time at a stop, or adds a stop. A new stop is placed in line order |
| `remove_stops` | `["VVH"]` | Drops stops |
| `stops` | `[["CSMT", "08:04"], ["BY", "08:11"]]` | Replaces every stop. Use this or the two above, not both |

**`action = "add"`** supplies a train the converter doesn't have, for example one it
rejected. It needs `direction` (`"up"` towards CSMT, or `"down"`), `service_type` and
`stops`, listed in the order the train calls. `days` defaults to `daily`.

```toml
[[train]]
number = "95999"
action = "add"
reason = "Listed in the CR notice of 2026-11-01, not yet in any PTT PDF."
direction = "down"
service_type = "fast"
days = "mon-sat"
service_code = "K 99"
stops = [["CSMT", "10:00"], ["DR", "10:12"], ["TNA", "10:40"], ["KYN", "11:02"]]
```

**`action = "remove"`** drops a train. It takes only `number` and `reason`.

Station codes are the ones in `cr_pdf.py` (`KYN`, `TNA`, `DR`, `CSMT`, ...). Times are
24-hour `HH:MM`, Mumbai time. As in the PDFs, a stop has one time: the converter writes it
as the departure at the first stop, the arrival at the last, and both in between.

### Validation

The whole file is checked before anything is applied, and every problem is listed at once
with the entry it is in:

```
error: timetable_overrides.toml has 2 problem(s):
  entry 1 (train 96415): '14:91' at DIVA is not a time like 08:04
  entry 3 (train 95999): action "add" needs direction
```

Refused: unknown keys, a missing reason, an unknown station code, a time that isn't one, two
entries for the same train, `set` or `remove` for a train that doesn't exist, `add` for one
that does, a stop to remove that the train doesn't have, and any change that leaves a train
with fewer than two stops, times out of order, or a run of 12 hours or more. If any entry
can't be applied, none is, and the conversion stops.

An entry that changes nothing, because a newer PDF now agrees with it, is not an error. The
converter says "no change; the timetable already says this", which is your cue to delete it.

### Using it

```bash
uv run python -m sitt.ingest.cr_pdf dn.pdf up.pdf --ac-supplement ac.pdf --15-car-supplement cars.pdf -o central.csv
```

The converter prints one line per override applied, and the CSV's header comment records how
many there were. Then load the CSV as usual.

| Option | Meaning |
| --- | --- |
| `--overrides PATH` | Use another overrides file instead of the shipped one |
| `--no-overrides` | Apply none |

### Stray marks in the PDF

The converter itself tolerates two kinds of typing slip, and reports each as a warning:

- a time with a stray mark beside it (`` 08:24` ``) is read as the time;
- a cell holding nothing but a stray mark, between two of a train's stops, is read as "passes
  without stopping", **but only if the train passes other stations too**.

The second is train 95901 (T 15, the 08:04 AC fast from CSMT to Thane). Its Vidyavihar cell
is a backtick where every neighbouring cell is `…`, and it used to be rejected. In an
all-stops train a lone stray mark is more likely what is left of a stop's time, so that train
is still rejected, and needs an override.

## Station codes

The codes come from `_STATIONS` in `cr_pdf.py`. All but four were checked against NTES's
station list. The four on the Khopoli branch beyond Palasdhari are **not verified against an
official source**:

| Station | Code | What was found (2026-10-05) |
| --- | --- | --- |
| Kelavli | `KLY` | Not in NTES's station list. Wikipedia and IndiaRailInfo give `KLY` |
| Dolavli | `DLV` | Not in NTES's station list. Wikipedia and IndiaRailInfo give `DLV` |
| Lowjee | `LWJ` | Not in NTES's station list. Wikipedia and IndiaRailInfo give `LWJ` |
| Khopoli | `KHPI` | Not in NTES's station list. Wikipedia and booking sites (ixigo, Goibibo) give `KHPI` |

NTES has none of the four under any code or spelling, and neither does the Indian Railways
GTFS feed built from it. The third-party sources all agree with the codes used here, so they
are probably right, but none is official. The risk is small: nothing matches on these codes
except this project's own timetable. A wrong one would only matter if a live source reported
a train at one of these stations under a different code, and `sitt-dq` would then list it
under "Station codes not in the timetable".

## Holidays

On some public holidays Central Railway runs the Sunday timetable. Its list has six fixed
dates and eight movable days a year:

| Fixed | Movable ("as per calendar") |
| --- | --- |
| Republic Day (26 Jan), Dr Ambedkar Jayanti (14 Apr), Maharashtra Day (1 May), Independence Day (15 Aug), Gandhi Jayanti (2 Oct), Christmas (25 Dec) | Holi (2nd day), Gudi Padwa, Good Friday, Ramzan-Id, Ganesh Chaturthi, Dassera, Diwali (1st and 2nd day) |

The fixed dates are in [`src/sitt/holidays.py`](../src/sitt/holidays.py). The movable ones
are in [`src/sitt/holidays.toml`](../src/sitt/holidays.toml), one entry per date:

```toml
[[holiday]]
date = 2026-10-20
name = "Dassera"
status = "reported"
source = "Maharashtra public holidays 2026 (GAD notification), ...; checked 2026-10-05"
```

| Status | Meaning | Used? |
| --- | --- | --- |
| `confirmed` | Seen in an official document, or fixed by calculation (Good Friday) | yes |
| `reported` | Several sources quote the official notification with the same date, but the official document itself wasn't opened | yes |
| `tentative` | Not officially notified yet. From calendars, and may be a day out | **no**, unless `SITT_HOLIDAYS_INCLUDE_TENTATIVE=true` |

What is in the file today:

- **2026:** all eight days, from the Maharashtra government's 2026 list. They are `reported`,
  not `confirmed`: on 2026-10-05 the official pages couldn't be opened, so the dates rest on
  a search-result extract of an official page and on third-party copies of the notification,
  which agree. Good Friday is `confirmed` by calculation.
- **2027:** all eight days as `tentative`, except Good Friday. Maharashtra's 2027 list is
  expected in November or December 2026. Two of them (Gudi Padwa, Dassera) have sources
  that disagree by a day; each entry's `note` says so.

One reading is this project's own: the railway says "Diwali 1st & 2nd day" and the state
lists two Diwali holidays, Laxmi Pujan and Bali Pratipada. They are taken to be the same two
days. The entries' notes flag this.

To correct or add a date, edit `holidays.toml`. A date written wrongly, a missing source or
a duplicate stops the program at start-up with a message naming the entry.

Holidays affect: which trains `/next` and `/commute` list, how crowd reports are matched to
trains, the `sunday_schedule` feature of the delay model, the crowding estimate's peak
scaling, and whether the bot's morning message and crowd prompt are sent.

One limit: the timetable CSV can't say "not on holidays" for a single train. A holiday is
treated as a Sunday for every train.
