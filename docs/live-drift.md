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
run gets a sequence of readings as it approaches. A *run* is one train at one station on one
service day; its arrival and departure readings are the same run.

Readings every 15 minutes rarely catch the moment a train passes: only 41 of the 2,227 real
readings are "actual" times. So **"what happened" is approximated by the last reading of the
run, if it was taken within 15 minutes of when the train was then expected.** Runs never
read that close to their time are left out.

For every earlier reading of such a run:

- **lead**: how long before the train's **scheduled** time the reading was taken. That is the
  only time a rider knows when the reading comes in. Readings taken after the scheduled time
  (the train hasn't come yet) get their own row;
- **drift**: the final delay minus the delay that reading gave. Positive means the train
  ended up later than the reading said.

## Results (first real data)

Data: 2,227 NTES readings at Kalyan, 5 October 20:00 to 6 October 12:45 2026 (two collection
sessions, about 17 hours), 1,155 of them suburban. These are small numbers from one station
on one weekday: read them as a first look, not as settled figures.

Suburban trains:

| Reading said | Taken before scheduled time | Readings | Mean abs drift | 10th pct | Median | 90th pct | Ended 5+ min later | Ended 3+ min earlier |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| on time | after its time | 76 | 7.6 | +0 | +0 | +20 | 22% | 0% |
| on time | 0-15 min | 88 | 7.6 | +0 | +0 | +20 | 39% | 0% |
| on time | 15-30 min | 109 | 6.8 | +0 | +0 | +20 | 33% | 0% |
| on time | 30-60 min | 190 | 7.0 | +0 | +0 | +22 | 37% | 0% |
| on time | 60+ min | 269 | 7.2 | +0 | +0 | +24 | 35% | 0% |
| late | after its time | 17 | 3.6 | +0 | +3 | +8 | 29% | 0% |
| late | 0-15 min | 9 | 4.9 | +2 | +6 | +8 | 56% | 0% |
| late | 15-30 min | 7 | 7.4 | +4 | +8 | +10 | 86% | 0% |
| late | 30-60 min | 5 | 8.4 | +6 | +8 | +12 | 80% | 0% |

With long-distance trains included, the pattern is the same, only larger: a long-distance
train read as late 30+ minutes before its time ended a median 13–16 minutes later still.

## What it shows

1. **An "on time" reading says little, however close to the train's time.** A third or more
   of suburban trains read "on time" before their scheduled time ended 5+ minutes late, and
   that share is about the same 60 minutes ahead as in the last 15 minutes (33–39%). NTES
   shows a train as on time until it is about to arrive, then the delay appears.
2. **Delays grow; they almost never shrink.** No suburban reading was followed by a train
   3+ minutes *earlier* than it said. A delay in a reading is a floor, not an estimate: the
   median train read as late before its time ended 6–8 minutes later still.
3. **"On time" after the scheduled time** usually means the train has come on time (a true
   reading), but 22% of them ended 5+ minutes late: NTES hadn't caught up.

## What the recommender does with it

- A live reading of **0 minutes late (or early) never overrides** the prediction: the train
  keeps the usual prediction (the typical past delay, or the timetable), and the reply says
  how many trains NTES showed as on time.
- A reading that **already shows a delay always counts**, as the train's delay leaving your
  station. It is treated as a floor: `/why` says delays read ahead usually grow.
- It does **not** yet add the typical drift (+6 to +8 minutes) to delayed readings, or put a
  range on them: with 21 "late" readings before their time, that is too thin. Re-run the
  command once there is more data; if the pattern holds, adding the median drift for the
  lead is the obvious next step.

(An earlier version measured the lead from the train's *expected* time, which already
includes its delay. That made on-time readings close to the train look reliable (16% ended
late, instead of 39%), and the recommender briefly trusted on-time readings within 15
minutes of a train. Found while fixing the same mistake in the
[line-state backtest](line-state.md).)

## Caveats

- One station (Kalyan), one weekday morning and evening. Peak/off-peak, direction and
  weekends are not separated: there aren't enough readings.
- The "final" reading is itself an expected time up to 15 minutes before the train; the
  true delay could still have grown after it. So the drift figures are, if anything, low.
- Runs are grouped by service day using the reading's own expected time; a train reported
  more than about 3 hours late around midnight could be split in two.
