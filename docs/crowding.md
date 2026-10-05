# Crowding estimate

Nobody publishes how full Mumbai locals are ([`data-sources.md`](data-sources.md), section 3).
So the crowding score is an **estimate from rules of thumb**, not a measurement and not a
trained model. It lives in [`src/sitt/models/crowding.py`](../src/sitt/models/crowding.py).

The score uses the same scale as the bot's `/log`:

| Score | Meaning     |
| ----- | ----------- |
| 1     | empty       |
| 2     | seats free  |
| 3     | standing    |
| 4     | packed      |
| 5     | can't board |

## The rules

Start at 2 (seats free) and add:

| Proxy | Change | Reasoning |
| --- | --- | --- |
| Leaves the boarding station in the peak for its direction (08:00–11:00 towards CSMT, 17:30–21:00 away from it) | +2 | Commuter flow |
| Within an hour either side of that peak | +1 | |
| Runs against the flow during the other direction's peak | +0.5 | |
| Between 23:00 and 05:00 | −0.5 | |
| Fast train, in its peak | +0.5 | Fast trains fill first |
| 15 cars | −0.5 | A quarter more room |
| Air-conditioned on that run | −1 | Dearer tickets, fewer riders |
| Starts at the boarding station | −1 | It arrives empty |
| Ladies' special | −0.5 | Fewer people may board |
| The train before it is cancelled | +1 | Its passengers wait for this one |
| The train before it is 10 or more minutes late | +0.5 | |

On a Sunday-timetable day the peak terms count for 40%. The total is kept between 1 and 5 and
rounded to the nearest level, with exact halves rounding down.

Every number is a field of `CrowdingRules` in that file, and each is a guess. Change them
there. The reply always lists the reasons that applied, such as "peak hour towards CSMT,
fast train, 15 cars".

## Your own reports

Reports you log with `/log` are blended in when they are for a similar train:

- the same boarding station,
- the same type (fast or slow),
- a departure time within 30 minutes,
- logged in the last 90 days.

With `n` such reports whose mean level is `m`:

```
estimate = (1 − w) × rule score + w × m      where w = n / (n + 3)
```

| Reports | Weight on your reports |
| ------- | ---------------------- |
| 1       | 25%                    |
| 3       | 50%                    |
| 9       | 75%                    |
| 27      | 90%                    |

So a single report nudges the estimate, and the rules fade out as reports accumulate. Reports
are matched by time of day only, not by weekday or by exact train.

## What it doesn't know

- Real passenger counts.
- Crowding at stations along the way: the score is for boarding at one station.
- Festivals, exam days, cricket matches or weather.
- Which train is "before it" beyond the timetable. Without live data, the train-ahead rules
  never fire.
