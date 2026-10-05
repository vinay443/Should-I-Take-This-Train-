# Telegram bot setup

The bot is a personal crowd logger: you tell it how full your train was, and it stores a row in
`crowd_reports`. It runs locally in polling mode, so no public URL or webhook is needed.

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

## Troubleshooting

- **`Conflict: terminated by other getUpdates request`**: another copy of the bot is polling with
  the same token. Stop the other one.
- **Logging fails with a DuckDB lock error**: DuckDB lets only one process open a database file
  for writing at a time. The bot opens it only briefly per command, but a long-running script or
  notebook holding `data/sitt.duckdb` open will block it. Close that connection and try again.
- **The bot doesn't reply at all**: check the console. Messages from users not in
  `ALLOWED_USER_IDS` are logged as "Ignoring update from unauthorised user".
