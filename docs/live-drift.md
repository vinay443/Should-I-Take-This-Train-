# How live readings drift

When NTES says a train is "running 9 minutes late", how far is that from what really
happens? The answer decides how much a live reading should count in a recommendation
([`recommender.md`](recommender.md#live-readings)).

Reproduce the tables with:

```bash
uv run python -m sitt.models.live_drift          # suburban trains
uv run python -m sitt.models.live_drift --all    # long-distance trains too
```

## Method

NTES's live station board gives each train an *expected* (or, once it has passed, *actual*)
time at the station, and so a delay. The collector reads it every time it runs, so one train
run gets a sequence of readings as it approaches.

Readings every 15 minutes rarely catch the moment a train passes: only 41 of the 2,227 real
readings are "actual" times. So **"what happened" is approximated by the last reading of the
run, if it was taken within 15 minutes of when the train was then due.** Runs never read that
close to their time are left out.

For every earlier reading of such a run:

- **lead**: how long before the train was due the reading was taken;
- **drift**: the final delay minus the delay that reading gave. Positive means the train
  ended up later than the reading said.

## Results (first real data)

Data: 2,227 NTES readings at Kalyan, 5 October 20:00 to 6 October 12:45 2026 (two collection
sessions, about 17 hours), 1,155 of them suburban. These are small numbers from one station
on one weekday: read them as a first look, not as settled figures.

Suburban trains:

| Reading said | Taken before due | Readings | Mean abs drift | 10th pct | Median | 90th pct | Ended 5+ min later | Ended 3+ min earlier |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| on time | 0-15 min | 64 | 2.6 | +0 | +0 | +14 | 16% | 0% |
| on time | 15-30 min | 103 | 5.3 | +0 | +0 | +18 | 29% | 0% |
| on time | 30-60 min | 190 | 7.1 | +0 | +0 | +20 | 37% | 0% |
| on time | 60+ min | 316 | 10.1 | +0 | +0 | +26 | 45% | 0% |
| late | 0-15 min | 13 | 3.0 | +0 | +3 | +8 | 23% | 0% |
| late | 15-30 min | 10 | 5.1 | +0 | +5 | +9 | 50% | 0% |
| late | 30-60 min | 13 | 7.0 | +3 | +8 | +8 | 85% | 0% |
| late | 60+ min | 2 | 9.0 | +5 | +9 | +13 | 50% | 0% |

With long-distance trains included, the pattern is the same, only larger: a long-distance
train read as late 60+ minutes ahead ended a median 23 minutes later still.

## What it shows

1. **NTES shows a suburban train as on time until it is close.** 83–99% of suburban readings
   say 0, and a third of those taken 15–60 minutes ahead ended 5+ minutes late. An on-time
   reading far ahead mostly repeats the timetable.
2. **Delays grow; they almost never shrink.** No suburban reading was followed by a train
   3+ minutes *earlier* than it said. A delay in a reading is a floor, not an estimate.
3. **The further ahead, the less a reading tells you**, which is what you would expect, but
   the size matters: a 90th percentile of +14 minutes even within 15 minutes of the train.

## What the recommender does with it

- A live reading of **0 minutes late counts only if taken within 15 minutes** of the train's
  scheduled time at your station (`LIVE_ON_TIME_TRUST_MINUTES` in
  [`recommend.py`](../src/sitt/recommend.py)). Further ahead, the train keeps the usual
  prediction, and the reply says how many trains NTES showed as on time.
- A reading that **already shows a delay always counts**, as the train's delay leaving your
  station. It is treated as a floor: `/why` says delays read ahead usually grow.
- It does **not** yet add the typical drift to the prediction, or put a range on it. With 38
  "late" readings in total, the medians above (+3 to +9 min) are too thin to build into every
  answer. Re-run the command once there is more data; if the pattern holds, adding the median
  drift for the lead is the obvious next step.

## Caveats

- One station (Kalyan), one weekday morning and evening. Peak/off-peak, direction and
  weekends are not separated: there aren't enough readings.
- The "final" reading is itself an expected time up to 15 minutes before the train; the
  true delay could still have grown after it. So the drift figures are, if anything, low.
- Runs are grouped by service day using the reading's own expected time; a train reported
  more than about 3 hours late around midnight could be split in two.
