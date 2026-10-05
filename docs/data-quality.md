# Data quality checks

`sitt-dq` looks through the collected observations for readings that shouldn't be trusted,
and for signs that the timetable or the station list is missing something. It lives in
[`src/sitt/dq.py`](../src/sitt/dq.py).

**Nothing is ever deleted or changed in `observations`.** A doubtful reading is *flagged*,
and the delay model leaves flagged readings out.

Synthetic observations are not checked. The report is about real data only.

## Running it

```bash
uv run sitt-dq
```

| Option | Meaning |
| --- | --- |
| `--hours N` | Report on the last `N` hours only. The default is every observation |
| `--markdown [PATH]` | Also write the report as Markdown. Without a path: `scratch/dq-report.md`, which is gitignored |
| `--write-flags` | Store the flags in the `dq_flags` table (see [below](#flags-in-the-database)) |
| `--db PATH` | A database other than `SITT_DB_PATH` |

Without `--write-flags` the database is opened read-only. Either way, if the collector has
the file at that moment, `sitt-dq` waits and retries a few times, then says the database is
busy and exits with code 2.

The first two lines of the report also appear in `sitt-health`
([`collector.md`](collector.md#8-the-health-report)), under **Data quality**.

## What is checked

### Row flags

Each of these marks individual readings. Bounds are in minutes and can be changed in `.env`.

| Flag | A reading gets it when | Setting (default) |
| --- | --- | --- |
| `exact_duplicate` | The same source, train, station, event, times and delay are already stored. The first copy is kept unflagged | |
| `near_duplicate` | The same source, train, station and event were read by a *different batch* within a few minutes. This is what two collectors running at once looks like. The later reading is flagged | `SITT_DQ_NEAR_DUPLICATE_MINUTES` (5) |
| `implausible_delay` | The delay is outside the allowed range. A train in the timetable may be up to 15 minutes early or 180 late. Any other train is taken to be long-distance and may be 120 early or 1,440 late | `SITT_DQ_MAX_EARLY_MINUTES` (15), `SITT_DQ_MAX_DELAY_MINUTES` (180), `SITT_DQ_MAX_EARLY_MINUTES_LONG_DISTANCE` (120), `SITT_DQ_MAX_DELAY_MINUTES_LONG_DISTANCE` (1440) |
| `delay_jump` | Between two consecutive readings of the same train at the same station, no more than an hour apart, the delay changed by more than the time that passed plus 15 minutes. A train can't lose 46 minutes in 15, or win them back | `SITT_DQ_JUMP_WINDOW_MINUTES` (60), `SITT_DQ_JUMP_SLACK_MINUTES` (15) |
| `schedule_mismatch` | The source's delay and the delay our timetable implies for the same clock time differ by more than 20 minutes. Either the timetable is wrong for that train, or the reading is matched to the wrong train | `SITT_DQ_SCHEDULE_MISMATCH_MINUTES` (20) |
| `far_from_schedule` | The reading was taken more than 4 hours from when the train was due at the station, allowing for its delay. A sign of a wrong-day match | `SITT_DQ_FAR_FROM_SCHEDULE_MINUTES` (240) |
| `future_timestamp` | It is stamped more than 5 minutes later than the moment of the check | `SITT_DQ_FUTURE_TOLERANCE_MINUTES` (5) |
| `batch_time_mismatch` | It is stamped more than 90 minutes from its batch's own time (the time in the batch ID). A difference of about 330 minutes is a UTC/IST mix-up | `SITT_DQ_BATCH_TIME_TOLERANCE_MINUTES` (90) |

The two schedule checks need the train to be in the timetable and to stop at that station, so
they say nothing about long-distance trains. Clock times are compared within a day, so they
work across midnight.

Checks that compare a reading with the one before it always look at all the data, even when
`--hours` limits what the report counts.

### Reported, not flagged

- **Train numbers not in the timetable.** Split in two. A number from 95000 to 99999 is a
  Central Railway suburban train, so one of those missing means the timetable is out of
  date. Anything else is a long-distance train: NTES lists those at Kalyan, and they are
  expected to be unmatched.
- **Station codes not in `stations`.** For Mobond, a station name the alias map
  (`src/sitt/ingest/live/stations.py`) doesn't know.
- **Rates per source**: readings, share cancelled, share marked less accurate, share with no
  delay.
- **Schedule differences**: trains whose source schedule differs from our timetable by 2
  minutes or more, but not enough to flag. A steady difference means the timetable has
  drifted for that train.

## Flags in the database

```bash
uv run sitt-dq --write-flags
```

This fills the `dq_flags` table:

| Column | Meaning |
| --- | --- |
| `observation_id` | `observations.id` of the suspect reading |
| `flag` | One of the row flags above |
| `detail` | The numbers behind it, e.g. `delay went from 2.0 to 60.0 min in 15.0 min` |
| `flagged_at` | When the flags were computed |

`dq_flags` is derived, so it is replaced each time: a flag that no longer applies, because a
bound changed or the timetable was fixed, goes away. `--write-flags` always covers every
observation, whatever `--hours` says. It needs to write, so it briefly takes the database the
way the collector does.

To see the flagged readings:

```sql
SELECT o.train_number, o.station_code, o.event, o.delay_minutes, f.flag, f.detail
FROM dq_flags f JOIN observations o ON o.id = f.observation_id
ORDER BY o.observed_at DESC;
```

### How the model uses them

`sitt.models.features.prepare` leaves out every observation that has a row in `dq_flags`.
Pass `exclude_flagged=False` to keep them. A database with no `dq_flags` table, or an empty
one, has nothing flagged, so nothing changes until you run `sitt-dq --write-flags`.
`sitt-retrain` ([`model-results.md`](model-results.md)) recomputes the flags before it builds
features.

The recommender's fallback, the median past delay of a train, does not yet look at the flags.

## Limits

- The bounds are guesses made with a few hours of real data. Review them once there are a
  few weeks.
- A reading can be wrong without being implausible. Nothing here catches a delay that is
  off by five minutes.
- `schedule_mismatch` can't tell a stale timetable from a mismatched train. Look at the
  examples: the same train flagged at every poll, with the same difference, is the timetable.
