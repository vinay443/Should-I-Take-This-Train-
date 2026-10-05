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

So a single report nudges the estimate, and the rules fade out as reports accumulate. These
"similar train" reports are matched by time of day only, not by weekday or by exact train.

### Reports for the exact train

A report that has been matched to a scheduled train (see the next section) says more about
that train than a neighbour's report does. When the train being scored has reports of its own,
logged at the same boarding station in the last 90 days:

- each of its own reports counts as one report;
- each merely similar report counts as half a report, in both `n` and the mean `m`.

A train with no reports of its own is scored exactly as before, from similar reports alone.
The half is `report_similar_weight` in `CrowdingRules`. `/why` says which kind was used:
"your 2 reports for this train and 3 for similar trains average 3.8".

## Matching a report to a train

A report says "the 8:12 fast from Kalyan was packed". To learn about one particular train,
that has to become a `train_id`. [`src/sitt/matching.py`](../src/sitt/matching.py) does it,
and **prefers leaving a report unmatched to matching it to the wrong train.**

### What you can tell it

`/log` takes a few optional words that narrow the train down. They can go anywhere in the
message:

| Words | Meaning |
| --- | --- |
| `to CSMT`, `to Kanjur Marg` | Where the train was going. It must call there after your station, which settles the direction |
| `ac`, `non-ac` | Whether it was an air-conditioned train |
| `15car`, `12car` | The rake length |
| `ladies special` | It was a ladies' special |

For example: `/log 8:12 fast KYN packed to CSMT`.

### How a match is made

1. **When.** The time you give is a clock time. It is taken as the most recent such time no
   later than an hour after you logged it. A report typed at 00:10 about the 23:50 belongs to
   yesterday. One typed on the platform a few minutes before the train belongs to today.
2. **Which trains ran.** Trains that call at the station that day and go on from it; a train
   that ends there can't be boarded. Running days follow the day the train starts its run,
   so the 00:20 from Kalyan that left CSMT at 23:20 counts as the evening before. A holiday
   in [`src/sitt/holidays.py`](../src/sitt/holidays.py) runs the Sunday timetable.
3. **What you said** rules trains out: fast or slow, the destination, AC, the rake length. A
   ladies' special is a candidate only if you said so, or if `SITT_LADIES_SPECIAL_OK=true`.
   An AC train that runs without AC at weekends and on holidays is not AC on those days.
4. **Score.** Each remaining train scores `1 − minutes off ÷ 10`: 1 for the exact minute, 0
   at ten minutes.
5. **Confidence** is the best score minus half the second-best.
6. Below 0.6, the report is left unmatched.

| Situation | Best | Second | Confidence | Result |
| --- | --- | --- | --- | --- |
| One train, the exact minute | 1.0 | | 1.0 | matched |
| One train, 3 minutes off | 0.7 | | 0.7 | matched |
| Exact, with another train 4 minutes away | 1.0 | 0.6 | 0.7 | matched |
| Exact, with another train 3 minutes away | 1.0 | 0.7 | 0.65 | matched |
| Exact, with another train 1 minute away | 1.0 | 0.9 | 0.55 | **left unmatched** |
| Two trains 2 minutes either side | 0.8 | 0.8 | 0.4 | **left unmatched** |
| One train, 5 minutes off | 0.5 | | 0.5 | **left unmatched** |

At a busy station without a destination, trains in both directions compete, so many reports
will be left unmatched. Adding `to CSMT` fixes most of them.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SITT_MATCH_WINDOW_MINUTES` | 10 | A train this far from the reported time scores nothing |
| `SITT_MATCH_MIN_CONFIDENCE` | 0.6 | Below this, the report is left unmatched |
| `SITT_MATCH_FUTURE_MINUTES` | 60 | How long before a train leaves you may log it |
| `SITT_MATCH_CHOICE_WINDOW_MINUTES` | 20 | Trains within this of the reported time are offered as buttons |
| `SITT_MATCH_CHOICES` | 4 | How many trains to offer |

### In the bot

When you log a report, the bot matches it there and then:

- **Matched:** the reply adds "Matched to the 08:16 fast to CSMT." with a **Not that train**
  button. Tapping it lists the trains around that time. Tap the right one, or **None of
  these**.
- **Unclear:** the reply says it couldn't tell and lists the trains straight away.
- **Nothing near that time:** the report is logged and nothing more is said.

A train you pick yourself is never changed by anything automatic. Neither is "None of these".
If matching fails for any reason, the report is still saved.

### What is stored

Four columns of `crowd_reports`:

| Column | Value |
| --- | --- |
| `matched_train_id` | The `trains.train_id`, or NULL when no train was picked |
| `match_confidence` | 0 to 1. NULL when nothing was matched. 1.0 for a train you picked |
| `match_method` | `auto`: matched when logged. `backfill`: matched later by `sitt-match-logs`. `user`: you picked the train. `user_none`: you said it was none of them. `nudge`: logged from the after-commute prompt, which already knew the train. NULL: not tried yet |
| `matched_at` | When that was set |

### Matching older reports

```bash
uv run sitt-match-logs
```

This tries every report that hasn't been tried yet and prints what it did with each.

| Option | Meaning |
| --- | --- |
| `--dry-run` | Show the matches without storing them |
| `--rematch` | Also redo reports that were matched automatically before. Use it after loading a new timetable |
| `--db PATH` | A database other than `SITT_DB_PATH` |

Reports you matched yourself, and those from the after-commute prompt, are never touched.

## What it doesn't know

- Real passenger counts.
- Crowding at stations along the way: the score is for boarding at one station.
- Festivals, exam days, cricket matches or weather.
- Which train is "before it" beyond the timetable. Without live data, the train-ahead rules
  never fire.
