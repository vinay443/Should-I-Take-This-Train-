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

- `/log 8:12 fast KYN packed`: quick log. Words can be in any order. Stations accept codes or
  names (`KYN`, `kalyan`, `Thane`). Crowd accepts `1`–`5` or
  `empty` / `seats` / `standing` / `packed` / `can't board`.
- `/log`: guided flow with buttons for station, time (or type one), fast/slow and crowd.
  If a quick log is missing something, the bot asks only for the missing parts.
- `/mylogs`: your last 10 reports.
- `/next KYN CSMT`: placeholder, replies "Coming soon."
- `/cancel`: abandon a log in progress.

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
