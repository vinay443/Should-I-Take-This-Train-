# Telegram bot setup

The bot is a personal crowd logger and train picker: you tell it how full your train was, and
it stores a row in `crowd_reports`; you ask it which train to take, and it recommends one. It
runs locally in polling mode, so no public URL or webhook is needed.

## 1. Create the bot with BotFather

1. In Telegram, open a chat with [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Pick a display name (e.g. "Should I take this train") and a username ending in `bot`
   (e.g. `vinay_sitt_bot`).
3. BotFather replies with a token like `123456789:AA...`. Treat it as a password: anyone with it
   can control the bot. If it leaks, send `/revoke` to BotFather to get a new one.
4. Optional but sensible for a personal bot: send `/setjoingroups`, pick the bot, and choose
   **Disable** so it can't be added to groups.

You don't need to register the command menu. The bot sets it on startup.

## 2. Find your Telegram user ID

The bot only answers user IDs listed in `ALLOWED_USER_IDS`, and it refuses to start while that
list is empty. To find your ID, either:

- message [@userinfobot](https://t.me/userinfobot), or
- set `ALLOWED_USER_IDS=0`, start the bot (step 4), and send it `/start`. It replies
  "This is a private bot. Your Telegram user ID is …". Then put that number in `.env` and restart.

## 3. Set environment variables

```bash
cp .env.example .env
```

Then edit `.env`:

| Variable             | Example              | Notes                                              |
| -------------------- | -------------------- | -------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN` | `123456789:AA...`    | From BotFather. Required.                          |
| `ALLOWED_USER_IDS`   | `123456789`          | Comma-separated for more than one person. Required. |
| `SITT_DB_PATH`       | `data/sitt.duckdb`   | Optional. Default shown.                           |
| `SITT_LOG_LEVEL`     | `INFO`               | Optional.                                          |
| `SITT_MODEL_DIR`     | `models/delay`       | Optional. Where the trained delay model is.        |

`.env` is gitignored. Variables already set in your shell take precedence over it.

## 4. Run it

```bash
uv sync
```

```bash
uv run sitt-bot
```

`python -m sitt.bot` works too. The bot creates the database and schema if needed, then polls
Telegram until you press Ctrl+C.

Try it from your phone:

- `/log 8:12 fast KYN packed`: quick log. Words can be in any order. Crowd accepts `1`–`5` or
  `empty` / `seats` / `standing` / `packed` / `can't board`. For stations, see
  [Stations](#stations) below.
  Optional extras help find the exact train: `to CSMT`, `ac` or `non-ac`, `15car`, `ladies
  special`. The reply says which scheduled train it was matched to, with a button to correct
  it. See [`crowding.md`](crowding.md#matching-a-report-to-a-train).
- `/log`: guided flow with buttons for station, time, fast/slow and crowd. You can type the
  station or the time instead of tapping. If a quick log is missing something, the bot asks
  only for the missing parts.
- `/mylogs`: your last 10 reports.
- `/next KYN CSMT`: which of the next five trains to take. It gives a one-line recommendation
  ("Take the 08:12 fast: arrives 08:58 ±4 min, …"), then each train with its predicted
  arrival, an estimated crowding level, and tags for ladies' specials, AC and 15-car trains.
  It needs a timetable in the database (see the [README](../README.md#setup)).
- `/why`: explains the last `/next` recommendation: the predicted delay and crowding reasons
  for every train, and the rule that decided it.
- `/fav add work KYN CSMT 8:12 mon-fri`: save a route. See [Saved routes](#saved-routes).
- `/commute`: `/next` for your saved route, with no typing.
- `/cancel`: abandon a log in progress.

## How much to trust `/next`

Every `/next` reply ends by saying what its times are based on:

- **"Timetable times only"**: there is no delay model and no collected data. The times are
  the scheduled ones.
- **"…the typical past delay of each train"**: medians from observations in the database.
- **"…the delay model"**: a model trained on real observations with
  `python -m sitt.models train --db data/sitt.duckdb`.

Today the first applies: no real observations have been collected, so `/next` gives
timetable times. A model trained on synthetic data is ignored unless you set
`SITT_ALLOW_SYNTHETIC_MODEL=true` for testing. If you do, every reply says **SYNTHETIC**: the
delay figures then come from invented data and say nothing about real trains.

Crowding is always a rule-of-thumb estimate, sharpened by your own `/log` reports. See
[`recommender.md`](recommender.md) for the decision rule and its thresholds, and
[`crowding.md`](crowding.md) for the crowding rules.

## Saved routes

Save the routes you travel, so the bot can answer with one tap and can ask you about your
usual train.

```
/fav add work KYN CSMT 8:12 mon-fri
/fav add home CSMT KYN 18:05
```

The name comes first (up to 20 letters, digits, `-` or `_`), then where from and where to,
typed any way `/next` accepts. Two things are optional and can come in either order:

- **the time of your usual train**, e.g. `8:12` or `6:05pm`;
- **the days you travel**: `mon-fri` (the default), `mon-sat`, `daily`, or a list such as
  `mon,wed,fri`.

Saving a name again replaces that route.

| Command | What it does |
| --- | --- |
| `/fav` or `/fav list` | Your saved routes |
| `/fav remove work` | Forget one |
| `/fav default work` | The route `/commute` uses. Your first saved route is the default |
| `/fav notify work off` | Stop the message before your usual train (`on` to restart) |
| `/fav nudge work off` | Stop the crowding question after it (`on` to restart) |

Favourites are per Telegram user and live in the `favourite_routes` table.

### `/commute`

`/commute` is `/next` for your default route. If you have also saved the same route the other
way round, the two are treated as an outbound and a return leg: before 14:00 you get the
outbound one, and from 14:00 the return one. The outbound leg is whichever has the earlier
usual train. `/why` explains the answer as usual.

### What the bot sends by itself

A saved route **with a usual train time** switches on two messages, on the days you said you
travel:

1. **Before your train.** 20 minutes before the usual time, the `/commute` recommendation
   arrives without you asking.
2. **After your train.** 5 minutes after your usual train's scheduled arrival, the bot asks
   "How crowded was the 08:12 fast to CSMT from Kalyan (KYN)?" with buttons `1` to `5`. One
   tap stores a crowd report that is already tied to that exact train (`match_method =
   'nudge'`), which is the most useful kind for the crowding estimate
   ([`crowding.md`](crowding.md#reports-for-the-exact-train)). **Didn't take it** logs
   nothing. **Stop asking** turns the question off for that route.

"Your usual train" is the scheduled train leaving within 10 minutes of the time you gave. If
there is none, `/fav add` says so and no question is asked.

Things to know:

- **They only fire while the bot is running.** The bot is a program on your PC, not a
  service. If `sitt-bot` isn't running, or the PC is asleep, nothing is sent. A message whose
  moment was missed by more than 10 minutes is skipped, not sent late.
- **Nothing is sent on Sundays or holidays** (the days in
  [`src/sitt/holidays.py`](../src/sitt/holidays.py)), unless you set
  `SITT_COMMUTE_SKIP_SUNDAY_SCHEDULE=false`.
- **Only users in `ALLOWED_USER_IDS` are ever messaged.**
- Each message goes out once a day per route, even if the bot restarts. This is recorded in
  the `bot_notifications` table.
- A train that arrives after midnight is asked about after it arrives, as part of the day it
  left on.

They need python-telegram-bot's job queue, which `uv sync` installs.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SITT_COMMUTE_RETURN_AFTER` | `14:00` | From this time `/commute` gives the return leg |
| `SITT_COMMUTE_NOTIFY` | true | Send the recommendation before the usual train |
| `SITT_COMMUTE_NOTIFY_LEAD_MINUTES` | 20 | How long before |
| `SITT_COMMUTE_NUDGE` | true | Ask how crowded the usual train was |
| `SITT_COMMUTE_NUDGE_AFTER_MINUTES` | 5 | How long after its scheduled arrival |
| `SITT_COMMUTE_SKIP_SUNDAY_SCHEDULE` | true | Send neither on Sundays and holidays |
| `SITT_COMMUTE_GRACE_MINUTES` | 10 | How late a missed message may still be sent |
| `SITT_COMMUTE_USUAL_TRAIN_MINUTES` | 10 | How near your usual time the train must leave |

## Stations

The bot reads its stations from the `stations` table, which the timetable loader fills. With
the real Central line timetable loaded, every station from CSMT to Kasara and Khopoli works in
both `/log` and `/next`. Before any timetable is loaded, the bot falls back to a built-in list
covering CSMT to Kalyan, so `/log` works straight away.

A station can be typed as:

- its code: `KYN`, `TNA`, `ULNR`
- its name, in any case, with or without spaces: `kalyan`, `Kanjur Marg`, `ulhasnagar`
- an alias: `vt` or `cst` for CSMT, `dombivali`, `kanjur`, `kalwa`, `diwa`, `sin` for Sion,
  `ambarnath`, `titvala` and a few more

Aliases live in `ALIASES` in [`src/sitt/bot/stations.py`](../src/sitt/bot/stations.py) and are
checked before codes and names. Add to that map when a spelling you use isn't recognised.

## What gets stored

Each report is one row in `crowd_reports`:

| Column              | Value                                                      |
| ------------------- | ---------------------------------------------------------- |
| `reported_at`       | When you sent it (UTC)                                     |
| `station_code`      | Boarding station, e.g. `KYN`                               |
| `train_description` | e.g. `08:12 fast from KYN`, to be resolved to a `train_id` |
| `crowd_level`       | 1 (empty) to 5 (can't board)                               |
| `source`            | `telegram:<your user id>`                                  |
| `note`              | The raw message, e.g. `/log 8:12 fast KYN packed`          |
| `matched_train_id`, `match_confidence`, `match_method`, `matched_at` | The scheduled train it was matched to, if any. See [`crowding.md`](crowding.md#what-is-stored) |

## Troubleshooting

- **`Conflict: terminated by other getUpdates request`**: another copy of the bot is polling with
  the same token. Stop the other one.
- **Logging fails with a DuckDB lock error**: DuckDB lets only one process open a database file
  for writing at a time. The bot opens it only briefly per command, but a long-running script or
  notebook holding `data/sitt.duckdb` open will block it. Close that connection and try again.
- **The bot doesn't reply at all**: check the console. Messages from users not in
  `ALLOWED_USER_IDS` are logged as "Ignoring update from unauthorised user".
