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

For each suburban run with a final reading (its last reading within 15 minutes of its
time), pretend to ask 15, 30, 45 or 60 minutes before that time, using only readings taken
by then, and predict its final delay four ways:

| Predictor | What it uses |
| --- | --- |
| zero | The timetable: on time |
| own | The run's own latest reading so far (0 if none) |
| line | Median delay of *other* trains in the same direction, read in the last 30 minutes within 15 minutes of their own time (0 if none) |
| max | The larger of own and line |

Nothing is fitted, so this can't overfit; on a small sample it can still be luck.

## Results (first real data: Kalyan, about 17 hours, 134 suburban runs)

| Asked before due | Runs | Line known | MAE zero | MAE own | MAE line | MAE max | ≤5 own | ≤5 max |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 15 min | 134 | 87% | 6.7 | 5.7 | 6.1 | **5.3** | 69% | 66% |
| 30 min | 134 | 78% | 6.7 | 5.8 | 6.2 | **5.5** | 68% | 67% |
| 45 min | 134 | 74% | 6.7 | 6.4 | 6.1 | **5.9** | 66% | 68% |
| 60 min | 134 | 66% | 6.7 | 6.5 | 6.3 | **6.2** | 66% | 64% |

By direction, all of the difference is in **up** trains (towards CSMT). Down trains were
almost all on time, so every predictor scores the same (2.3 min). Up trains, 15 minutes
ahead: own 9.4, line 10.3, max **8.7** minutes; 60 minutes ahead: own 11.3, line 10.8, max
**10.6**.

Split by session, it is not consistent. On the Tuesday morning "max" was clearly better for
up trains (15 min ahead: 10.8 → 8.9); on the Monday evening it was worse at 15 minutes
(7.6 → 8.5), and its share within 5 minutes fell from 41% to 19%.

## Decision: not used in recommendations

`max` has the lowest average error in every row, but by 0.3–0.5 minutes, in one direction,
and not in both sessions. The share of trains predicted within 5 minutes, which is what
a rider feels, is no better. That isn't the clear win needed to change every answer, so the
recommender doesn't use it.

What would change that: with a few more weeks of hand-collected readings, if `max` keeps
beating `own` for up trains in most sessions, add the line state as a floor on predicted
delay for trains without an informative reading of their own. The code to get it is the
`line` query in [`line_state.py`](../src/sitt/models/line_state.py).

## Caveats

Same as [`live-drift.md`](live-drift.md): one station, two sessions on two days, final
readings that are themselves up to 15 minutes early. And at Kalyan, NTES lists only trains
going beyond Kalyan, so "the line" here is the Kasara/Karjat branch traffic, not the
CSMT–Kalyan locals.
