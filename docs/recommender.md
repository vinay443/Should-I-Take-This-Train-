# The recommender: take this train, or wait?

Given two stations and a time, [`src/sitt/recommend.py`](../src/sitt/recommend.py) looks at
the next few trains, predicts when each will really arrive and how crowded it will be, and
recommends one with a one-line reason:

> Take the 08:12 fast: arrives 08:58 ±4 min, likely packed, but the next best (08:27 fast)
> arrives 15 min later.

The bot's `/next` shows this above the list of trains, and `/why` explains it. The dashboard
has an interactive version.

## Where the predicted times come from

The recommender uses the best source it has, and every reply says which:

| Level | Used when | What it does |
| --- | --- | --- |
| **model** | A trained delay model is saved in `models/delay` | Predicts each train's delay at your station and at your destination, with a rough range (the "±4 min") |
| **baseline** | No model, but the database has observations | Uses the median delay seen on earlier days for that train at that station |
| **timetable** | Neither | Scheduled times as they stand |

**If the model was trained on synthetic data, or the observations behind a baseline are
synthetic, every reply says so.** Until real observations have been collected, that is the
case for any model you train: the delay figures are then invented and only the timetable
times are real. To run without a model, delete `models/delay` or point `SITT_MODEL_DIR` at
an empty folder.

Crowding always comes from the rule-of-thumb estimate in [`crowding.md`](crowding.md).

## The decision rule

1. Trains reported cancelled are out. So are ladies' specials, unless you set
   `SITT_LADIES_SPECIAL_OK=true`: the recommender doesn't know who is asking. They are still
   listed.
2. Start with the train predicted to **arrive first**.
3. If that train is running **very late** at your station (15 minutes or more), and another
   train that isn't very late arrives within 10 minutes of it, prefer the other. A badly
   delayed train's arrival time is the least certain.
4. If another train arrives **within 5 minutes** of the pick and is **at least one crowding
   level emptier**, prefer it. Among several, the emptiest, then the earliest to arrive.

The reply says "Wait for…" when following it means letting an earlier train go, and
"Take…" otherwise.

## Thresholds

Set these in `.env` (see [`.env.example`](../.env.example)). They are read by
`RecommendSettings` in [`src/sitt/config.py`](../src/sitt/config.py).

| Variable | Default | Meaning |
| --- | --- | --- |
| `SITT_RECOMMEND_CANDIDATES` | 5 | How many upcoming trains to compare |
| `SITT_WAIT_MAX_EXTRA_MINUTES` | 5 | An emptier train must arrive within this many minutes of the earliest |
| `SITT_CROWD_GAIN_LEVELS` | 1 | ...and be at least this many crowding levels emptier |
| `SITT_VERY_LATE_MINUTES` | 15 | Predicted delay at your station that counts as "very late" |
| `SITT_VERY_LATE_SLACK_MINUTES` | 10 | How soon after it another train must arrive to be preferred |
| `SITT_LADIES_SPECIAL_OK` | false | Whether ladies' specials may be recommended |
| `SITT_MODEL_DIR` | `models/delay` | Where the trained delay model is |

## What it can't do yet

- **Live data.** A train is known to be cancelled, or the train ahead to be late, only if the
  collector has stored readings in the database within the last few hours. The collector's
  schedule is not switched on, so in practice these rules never fire yet.
- **Changing trains.** It compares direct trains only.
- **Platforms, or which coach to board.**
