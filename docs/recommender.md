# The recommender: take this train, or wait?

Given two stations and a time, [`src/sitt/recommend.py`](../src/sitt/recommend.py) looks at
the next few trains, predicts when each will really arrive and how crowded it will be, and
recommends one with a one-line reason:

> Take the 08:12 fast: arrives 08:58 ±4 min, likely packed, but the next best (08:27 fast)
> arrives 15 min later.

The bot's `/next` shows this above the list of trains, and `/why` explains it. The dashboard
has an interactive version, and `uv run sitt-next KYN CSMT` prints it in a terminal (see the
README for its options).

## Where the predicted times come from

The recommender uses the best source it has, and every reply says which:

| Level | Used when | What it does |
| --- | --- | --- |
| **model** | A delay model **trained on real observations** is saved in `models/delay` | Predicts each train's delay at your station and at your destination, with a rough range (the "±4 min") |
| **baseline** | No usable model, but the database has observations | Uses the median delay seen on earlier days for that train at that station |
| **timetable** | Neither, or there are readings but none from earlier days of these trains | Scheduled times as they stand |

**A model trained on synthetic data is not used.** Until real observations have been
collected, any model you train comes from invented delays, so by default the recommender
ignores it and falls back to the timetable (or to past observations, if there are any). The
reply then notes that a model exists but is not used. This is the state of the project today:
`/next` gives timetable times.

**A real model is used only where it was trained.** A model built by `sitt-retrain`
([`retraining.md`](retraining.md)) lists the stations it was trained and evaluated on. NTES
reports locals beyond Kalyan, not CSMT–Kalyan locals, so that list will be short. For a
station outside it, the time comes from the typical past delay or the timetable, and the
reply says so: "The delay model covers Kalyan only. Times at Dadar use the typical past
delay." If neither of your stations is covered, the model isn't used at all and the level
named in the reply is the one that was.

To try a synthetic model anyway, for testing, set this in `.env`:

```
SITT_ALLOW_SYNTHETIC_MODEL=true
```

Every reply then says the predictions were built from synthetic data, and `/why` says the
delay figures tell you nothing about real trains. The same warning appears if the observations
behind a baseline are synthetic.

Crowding always comes from the rule-of-thumb estimate in [`crowding.md`](crowding.md).

## Live readings

Whatever the level, a **recent reading of a train today** beats a prediction. If the database
holds a reading of a run at your station, or at a station before it on that train's route,
taken no more than 30 minutes before the time you ask about (`SITT_LIVE_MAX_AGE_MINUTES`):

- if it shows a **delay**, that becomes the train's delay leaving your station, as a floor
  (delays read ahead of time usually grow). An **on-time or early** reading never overrides
  the prediction: NTES shows trains as on time until they are about to arrive, and a third
  of those read on time before their scheduled time ended 5+ minutes late, however close to
  that time the reading was ([`live-drift.md`](live-drift.md)). The reply says how many
  trains NTES showed as on time;
- the arrival keeps whatever change en route the level predicted. At timetable level that is
  none, so a train 9 minutes late leaving is shown 9 minutes late arriving. With a baseline of
  "on time at Kalyan, 14 late into CSMT" and a live reading of 5 late, it arrives 19 late;
- a train whose **scheduled time has passed but which is running late enough to still be
  caught** is put back on the list ("Running late, so still to leave: the 10:06 fast");
- the train just ahead's live delay feeds the crowding rule ("the train before it is 17 min
  late"), even at timetable level. "Just ahead" means on the same line: where the main and
  Harbour lines share stations (CSMT to Kurla) their trains leave from different platforms,
  so a late or cancelled Harbour train doesn't count for the next main line train.

Readings taken after the time you ask about are ignored, so asking about a past moment with
`sitt-next --date … --at …` doesn't see the future. Suspect readings flagged by `sitt-dq` are
left out. Live trains are marked: "· live" in `/next`, "(live 10:15)" in `sitt-next`, a Live
column in the dashboard, and a line in `/why`.

**Nothing collects in the background**, so readings are only as fresh as your last manual
run. To use them, collect once and then ask:

```bash
uv run sitt-next KYN CSMT --collect
```

That runs one `sitt-collect` round first (the same as running it yourself) and prints how it
went ("Live check just now: ntes 212 readings."). If the fetch fails, you still get a
recommendation from what the database already has.

NTES reports trains from beyond Kalyan (Kasara, Karjat and so on) and long-distance trains,
so live readings mostly help for journeys from Kalyan or further out.

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

The reply says "Wait for…" when following it means letting a train that leaves earlier go,
and "Take…" otherwise. "Earlier" is by when trains are expected to leave, not by the
timetable: a train 20 minutes late that leaves after the pick isn't one you wait through,
and an on-time train timetabled after a late pick but leaving before it is.

When no train in the timetable goes from one station to the other at all, the reply says
why instead of "No trains found": the stations are on different lines (Kalyan and Panvel:
"They meet at CSMT, Masjid, Sandhurst Road and Kurla: ask for each part separately"), or
the line's timetable for that direction isn't loaded (only the Harbour line's DOWN PDF is,
so nothing runs towards CSMT on it), or no single train calls at both (Bandra and Vashi,
on two branches of the Harbour line). The web page then shows only that sentence.

For stations on different lines it also names the quickest journey with one change, by
the timetable: "Quickest with one change on these two lines, by the timetable: the 08:04
fast, changing at Kurla (arrives 08:46) to the 08:53 slow, at Panvel 09:45. Ask for each
part to see delays and crowding." (Kalyan → Panvel, Monday 08:00, real timetable). It tries
the first 6 trains to each station where the lines meet, allows at least 5 minutes to
change (`CHANGE_MINUTES`, a guess) and at most 60 minutes of waiting, leaves out ladies'
specials, and picks the earliest arrival (on a tie, the one that leaves latest). It knows
only the Central and Harbour lines: for Dadar → Goregaon it suggests going via Sandhurst
Road, where in practice the Western line is quicker. Delays and crowding aren't used,
which is why it asks you to look at each part.

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
| `SITT_ALLOW_SYNTHETIC_MODEL` | false | Use a model even if it was trained on synthetic data. For testing only |
| `SITT_LIVE_MAX_AGE_MINUTES` | 30 | How old a reading may be and still count as live. 0 turns live readings off |

## What it can't do yet

- **Fresh data by itself.** Live readings and cancellations are only as recent as your last
  manual `sitt-collect`; nothing runs in the background. NTES doesn't cover CSMT–Kalyan
  locals, so most of the core route has no live readings at all.
- **Changing trains.** It compares direct trains only.
- **Platforms, or which coach to board.**
