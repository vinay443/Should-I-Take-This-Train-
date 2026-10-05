# Megablocks

A megablock is planned maintenance that closes a section of line for a few hours, usually on a
Sunday. Trains on the blocked tracks are diverted, delayed or cancelled. The `blocks` table
records them, the delay model uses them as a feature (`megablock`), and
`sitt.ingest.blocks` fills the table.

## What can be fetched, and what can't

No public web page gives a megablock's **section and times** in a form that can be fetched
reliably. Checked in October 2026:

| Source | What it has | Usable? |
| --- | --- | --- |
| [Yatri's announcements page](https://yatrirailways.com/live-railway-announcements) | A card per megablock with a title such as "Megablock on Central and Harbour line on Sunday, 27th September". The section, times and tracks are only inside the app. | Yes, for the **date and line** |
| Central Railway's press-releases page | Two unrelated releases; no megablock notices | No |
| X, news sites, third-party timetable sites | The full notice, as prose | Not fetched: paid API, or someone else's writing |

So fetching gives a **date-only block**: "there is a megablock somewhere on this line today".
The model treats every train on that line that day as affected. To record where and when,
enter the details by hand.

Only facts are stored: date, line, section, times, tracks, and a summary in this project's own
words. An announcement's text is never kept.

## Commands

`sitt-block` is installed with the project (`python -m sitt.ingest.blocks` also works). Each
command takes `--db` before the command name to use a database other than `data/sitt.duckdb`.

Fetch what Yatri lists. This makes one request:

```bash
uv run sitt-block fetch
```

Add `--dry-run` to see what it finds without storing anything.

Enter a block by hand. Only `--date` is required:

```bash
uv run sitt-block add --date 2026-10-11 --from MTN --to MLND --start 11:05 --end 15:55 --tracks fast --direction both
```

| Option        | Values                                                | Default   |
| ------------- | ----------------------------------------------------- | --------- |
| `--date`      | `YYYY-MM-DD`, the day the block starts                | required  |
| `--line`      | `central`, `harbour`, `transharbour`, `western`       | `central` |
| `--from`, `--to` | Station codes or names at the ends of the section  | not set   |
| `--start`, `--end` | `HH:MM`. An end before the start means past midnight | not set |
| `--tracks`    | `fast`, `slow`, `both`                                | not set   |
| `--direction` | `up`, `down`, `both`                                  | not set   |
| `--note`      | A short note in your own words                        |           |

Or paste the notice's wording and let it pick out the fields:

```bash
uv run sitt-block parse --date 2026-10-11 "Thane-Kalyan Up and Down slow lines from 10.40 am to 3.40 pm"
```

`parse` understands sentences of the form *station–station, Up/Down, fast/slow lines, from
time to time*, several in one text. It prints what it read. Check it, and use `--dry-run` to
try it without storing. Anything in another form needs `add`.

See and remove blocks:

```bash
uv run sitt-block list
```

```bash
uv run sitt-block remove 20261011-central-720536b0
```

A detailed block replaces the date-only one for the same day and line, and fetching again
doesn't bring the date-only one back.

## Fetching daily on GitHub

The **Fetch megablock announcements** workflow
([`blocks.yml`](../.github/workflows/blocks.yml)) runs `sitt-block fetch` and commits the
result as `data/blocks/<date>.json` on the `data` branch, next to the collector's files. It
starts by hand only: Actions → Fetch megablock announcements → Run workflow. To run it every
day, uncomment the `schedule` lines in the file and push.

To load what it has saved into your database, with the `data` branch checked out beside the
repository as described in [`collector.md`](collector.md):

```bash
uv run sitt-block load ../sitt-data/data/blocks
```

## How the model uses a block

A train is marked as affected (`megablock = 1`) at a station when a block:

- is on the train's service day and line,
- has no tracks recorded, or `both`, or the train's own type (fast or slow),
- has no section recorded, or the station lies within it,
- has no times recorded, or the train is scheduled at the station within them.

So the less a block says, the more trains it covers.
