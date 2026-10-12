# Line state: does the train ahead tell you about yours?

NTES shows a suburban train as on time until it is close
([`live-drift.md`](live-drift.md)), so for most trains you might take, their own reading
says little. But trains that are arriving right now *are* reported honestly. If the up
trains arriving at Kalyan this half hour are 12 minutes late, is yours likely to be too?

Reproduce with:

```bash
uv run python -m sitt.models.line_state
uv run python -m sitt.models.line_state --by-direction
```

## The backtest

A *run* is one train at one station on one service day (its arrival and departure readings
count once). For each suburban run with a final reading (its last reading within 15 minutes
of its expected time), pretend to ask 15, 30, 45 or 60 minutes before its **scheduled**
time, using only readings taken by then, and predict its final delay four ways:

| Predictor | What it uses |
| --- | --- |
| zero | The timetable: on time |
| own | The run's own latest reading so far (0 if none) |
| line | Median, over *other* trains in the same direction at the same station, of each one's latest reading in the last 30 minutes taken within 15 minutes of its own time (0 if none) |
| max | The larger of own and line |

Nothing is fitted, so this can't overfit; on a small sample it can still be luck.

## Results (first real data: Kalyan, about 17 hours, 134 suburban runs)

| Asked before due | Runs | Line known | MAE zero | MAE own | MAE line | MAE max | ≤5 own | ≤5 max |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 15 min | 134 | 87% | 6.7 | 6.3 | 6.3 | **6.2** | 67% | 60% |
| 30 min | 134 | 76% | 6.7 | 6.5 | 5.9 | **5.8** | 66% | 66% |
| 45 min | 134 | 69% | 6.7 | 6.6 | 6.2 | **6.2** | 66% | 63% |
| 60 min | 134 | 63% | 6.7 | 6.7 | **6.0** | **6.0** | 66% | 66% |

- **A train's own reading barely beats the timetable** (6.3–6.7 vs 6.7 minutes): asked
  before its scheduled time, NTES usually still shows it on time ([`live-drift.md`](live-drift.md)).
- **The line state is the better signal further ahead**: 5.9–6.0 minutes at 30–60 minutes.
- All of the difference is in **up** trains (towards CSMT): down trains were almost all on
  time, so every predictor scores 2.3 minutes. For up trains, `max` goes from 10.8 (own)
  to 10.5 minutes at 15 minutes ahead, and from 11.5 to 10.1 at 60.

Split by session, for up trains: on the Tuesday morning `max` beat `own` at every lead (15
min: 11.9 → 10.7); on the Monday evening it was **worse** at 15 minutes (9.3 → 10.2) and
better further ahead (60 min: 10.1 → 8.9). The share within 5 minutes was mostly no better,
and at 15 minutes clearly worse (Monday 37% → 15%).

## Decision: not used in recommendations

The gain is around half a minute on average, in one direction, not at every lead in both
sessions, and the share of trains predicted within 5 minutes (what a rider feels) doesn't
improve. That isn't the clear win needed to change every answer, so the recommender
doesn't use it.

What would change that: with a few more weeks of hand-collected readings, if `max` (or
`line`) keeps beating `own` for up trains 30+ minutes ahead in most sessions, use the line
state as a floor on predicted delay for trains without an informative reading of their own.
The code to get it is the `line` query in [`line_state.py`](../src/sitt/models/line_state.py).

(An earlier version of this page counted from the train's *expected* time, which already
includes its delay, so it partly knew the answer; it made `own` look better than it is.
Found by an independent review and fixed.)

## Caveats

Same as [`live-drift.md`](live-drift.md): one station, two sessions on two days, final
readings that are themselves up to 15 minutes early. And at Kalyan, NTES lists only trains
going beyond Kalyan, so "the line" here is the Kasara/Karjat branch traffic, not the
CSMT–Kalyan locals.
