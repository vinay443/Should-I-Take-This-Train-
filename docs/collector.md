# Live collector

The collector takes a snapshot of live running status from two sources and stores each
reading as a row shaped like the `observations` table. Why these two sources, and what each
can and can't tell you, is in [`data-sources.md`](data-sources.md).

| Source   | What one run fetches                                             | Requests |
| -------- | ---------------------------------------------------------------- | -------- |
| `mobond` | m-Indicator's live-train feed: every running local, with delay and cancellations | 1 |
| `ntes`   | The NTES Live Station board for Kalyan (next 2 hours)            | 3 (cookie, token, board) |

**Mobond is a private endpoint of a commercial app. Don't poll it until Mobond has agreed.**
The scheduled workflow keeps it switched off until you turn it on (see
[Turning on the schedule](#4-turning-on-the-schedule)).

## 1. Running it locally

```bash
uv run python -m sitt.ingest.live --source ntes --dry-run
```

`--dry-run` fetches and parses, prints a summary with five sample rows per source, and writes
nothing. It still makes real requests.

```bash
uv run python -m sitt.ingest.live --source ntes
```

| Option        | Default | Meaning                                                        |
| ------------- | ------- | -------------------------------------------------------------- |
| `--source`    | `all`   | `mobond`, `ntes` or `all`                                      |
| `--dry-run`   | off     | Fetch and parse, write nothing                                 |
| `--data-dir`  | `data`  | Where `observations/` and `raw/` are written                   |
| `--raw-dir`   |         | Put raw responses somewhere other than `DATA_DIR/raw`          |
| `--load`      | off     | Afterwards, import the new batches into the local DuckDB       |

A run that isn't a dry run writes:

```
data/raw/2026-09-27/20260927T102100Z-3fa2c1-mobond.json.gz    one per source, the response as received
data/raw/2026-09-27/20260927T102100Z-3fa2c1-ntes.json.gz
data/observations/2026-09-27/20260927T102100Z-3fa2c1.parquet  one per run, all sources together
```

The raw files keep everything the source sent. The Parquet file holds only the parsed fields
and leaves out the source's own text (see
[What is and isn't published](#what-is-and-isnt-published)).

Dates and times in file names are UTC. Both folders are gitignored on `main`.

The exit code is 1 if any source failed. Whatever the other sources returned is still written.

How it behaves towards the sources:

- Each source is fetched once per run. There are no retries: a source that fails waits for the
  next run.
- Requests time out after 20 seconds (10 to connect). The three NTES requests are a second apart.
- The User-Agent names the project and its polling rate. On GitHub Actions it also links the
  repository. Set `SITT_USER_AGENT` to replace it.
- The raw response is archived before it is parsed, so a parser bug doesn't lose data.
  `sitt.ingest.live.storage.read_raw` reads an archive back for re-parsing.

## 2. Loading observations into DuckDB

```bash
uv run python -m sitt.ingest.live.load
```

This reads every Parquet file under `data/observations/` into the `observations` table of
`data/sitt.duckdb`. Use `--dir` and `--db` for other locations. It is safe to re-run: a batch
that is already in the table is skipped.

After loading, it matches each reading to the timetable:

- `train_id` becomes the `trains.train_id` whose `number` equals the reading's `train_number`,
  when exactly one train has that number. Otherwise `train_id` stays as the raw number.
- A station stored by name (because the collector didn't know its code) becomes the matching
  `stations.code`.

Matching happens here rather than in the collector because the GitHub runner has no timetable
database. Run the loader again after loading a new timetable to match older rows.

To load what the scheduled workflow has collected, check out the `data` branch next to the
repository and point the loader at it:

```bash
git fetch origin data
```

```bash
git worktree add ../sitt-data data
```

```bash
uv run python -m sitt.ingest.live.load --dir ../sitt-data/data/observations
```

Later, update the worktree with `git -C ../sitt-data pull` and run the loader again.

## 3. The probe

GitHub-hosted runners have IP addresses outside India, and Indian Railways sites sometimes
block those. Before relying on the scheduled collector, check that the sources answer:

1. On GitHub, open **Actions â†’ Probe live sources â†’ Run workflow**.
2. Leave `sources` as `mobond ntes`, or enter just `ntes` to leave Mobond alone.
3. Read the **Probe** step's log. For each source it prints every request with its status code
   and size, the content type, a 400-character sample of the response, and how many
   observations were parsed.

The probe makes one fetch per source and stores nothing. The job fails if a source can't be
fetched or parsed. The same check runs locally with
`uv run python -m sitt.ingest.live.probe ntes`.

If NTES is blocked from the runners, run the collector from this machine instead: see
[Running it on this Windows machine](#6-running-it-on-this-windows-machine).

## 4. Turning on the schedule

The **Collect live data** workflow ([`collect.yml`](../.github/workflows/collect.yml)) can
only be started by hand until you do the following.

1. **Get Mobond's agreement**, or decide to collect NTES only.
2. **Run the probe** (above) and confirm the sources you want answer from a runner.
3. **Run the collector once by hand:** Actions â†’ Collect live data â†’ Run workflow. The first
   run creates the `data` branch. Check that it contains a Parquet file.
4. **Switch Mobond on, if agreed:** Settings â†’ Secrets and variables â†’ Actions â†’ Variables â†’
   New repository variable, with name `SITT_MOBOND_ENABLED` and value `true`. Without it, the
   workflow fetches NTES only, even if you choose `mobond` or `all` by hand. Delete the
   variable or set it to anything else to switch Mobond off again.
5. **Uncomment the schedule** in `collect.yml` and push to `main`:

   ```yaml
   on:
     schedule:
       - cron: "*/15 * * * *"
     workflow_dispatch:
   ```

To stop collecting, comment the schedule out again, or disable the workflow from its page in
the Actions tab.

Things to know about scheduled runs:

- GitHub starts them late when it is busy and sometimes skips them. Expect gaps.
- GitHub disables schedules in a repository with no activity for 60 days.
- This repository is public, so the `data` branch is too. See
  [What is and isn't published](#what-is-and-isnt-published).

## 5. How the data is stored

Each run writes one new Parquet file and commits it to a separate `data` branch. That branch
is an orphan: it shares no history with `main` and contains only `data/observations/`. Files
are never modified after they are written, so two runs can never conflict.

| Good                                                          | Bad                                                      |
| ------------------------------------------------------------- | -------------------------------------------------------- |
| Free, with no database server or storage account to run       | 96 commits and 96 small files a day at a 15-minute cadence |
| `main` stays small, and code history isn't buried in data commits | The branch only grows. Git keeps every file forever   |
| Every batch is versioned, and a bad one can be dropped by file name | Many small files are slower to read than a few big ones |
| DuckDB reads the files directly, with no import step needed for ad-hoc queries | Anyone who can see the repository can see the data |

A batch of about 290 rows is roughly 9 KB, so a year of 15-minute runs is on the order of
300 MB across 35,000 files. Before it gets that far, compact old days into one file per day or
month and rewrite the branch.

### What is and isn't published

This repository is public, so the `data` branch is too. Mobond's feed is a commercial app's
data, and [`data-sources.md`](data-sources.md) says not to republish it. So the Parquet files
hold **derived fields only**:

| In the Parquet files (public)                                        | Not in them                         |
| -------------------------------------------------------------------- | ----------------------------------- |
| train number, station code, event, delay, time, `cancelled`, `less_accurate`, source, batch | `raw_status`: the source's own text, e.g. Mobond's status string for a train |

`raw_status` is still a column of the `observations` table, but it is NULL for every row
loaded from the Parquet files. The text itself survives in two places:

- **Locally:** the raw archive under `data/raw/`, which holds each response exactly as
  received. It is gitignored.
- **On GitHub:** the same raw archive, uploaded as a workflow artifact on every run and
  deleted after 90 days. Artifacts of a public repository can be downloaded by anyone who is
  signed in to GitHub, so this is less exposed than the branch but not private.

One field can still carry a word of Mobond's text: when the collector doesn't recognise a
station name, `station_code` holds the name as Mobond wrote it (e.g. `LONAVLA`) until the
loader matches it to a code. Status text that the parser doesn't understand is stored as
event `unknown` with no station, so it never reaches the files.

Setting the repository variable `SITT_COMMIT_RAW=true` also commits raw responses to the
`data` branch, **except Mobond's**, which the workflow always leaves out. In practice that
means the NTES board pages.

## 6. Running it on this Windows machine

Use this if the probe shows GitHub's runners can't reach the sources, or if you simply want
the data in your local database without the `data` branch.

`sitt-collect` does one whole round: it fetches each enabled source once, writes the raw
responses and a Parquet batch under `data/`, and loads every batch not yet in
`data/sitt.duckdb`. Try it by hand first:

```bash
uv run sitt-collect
```

It prints what it fetched and loaded, and appends the same lines to
`data/logs/collector.log`. The exit code is 1 if a source failed or the load didn't happen.

**Mobond is off by default**, exactly as in the workflow. `sitt-collect` fetches NTES only
unless `.env` (or the environment) has:

```
SITT_MOBOND_ENABLED=true
```

Only the word `true` switches it on. Leave it off until Mobond has agreed.

### Every 15 minutes with Task Scheduler

The repository has two PowerShell scripts for this:

- [`scripts/collect-once.ps1`](../scripts/collect-once.ps1) changes to the repository folder
  and runs `uv run sitt-collect`. This is what the scheduled task calls.
- [`scripts/register-collector-task.ps1`](../scripts/register-collector-task.ps1) creates the
  scheduled task.

To set it up, open PowerShell in the repository folder and run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register-collector-task.ps1
```

That creates a task named **SITT live collector** which runs every 15 minutes while you are
signed in. It needs no administrator rights. `-ExecutionPolicy Bypass` applies to that one
command only; it lets PowerShell run a script from this repository without changing your
system's policy.

The task:

- runs under your account, hidden, and only while you are signed in;
- catches up once if the PC was asleep at the scheduled time;
- never starts a second run while one is still going;
- is stopped if a run takes more than 5 minutes.

Check that it works:

```powershell
Start-ScheduledTask -TaskName "SITT live collector"
```

```powershell
Get-Content data\logs\collector.log -Tail 10
```

To poll less often, add `-Minutes 30` to the registration command. The script refuses anything
under 15. To stop collecting:

```powershell
Unregister-ScheduledTask -TaskName "SITT live collector" -Confirm:$false
```

If you would rather click through it: open **Task Scheduler â†’ Create Task**. On **Triggers**,
add one that starts today and repeats every 15 minutes indefinitely. On **Actions**, start the
program `powershell.exe` with the arguments
`-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\path\to\repo\scripts\collect-once.ps1"`.
On **Settings**, choose "Do not start a new instance" if the task is already running.

Things to know:

- **`uv` must be on your PATH**, since the task runs `uv run`. If the log stays empty, run
  `scripts\collect-once.ps1` by hand to see the error.
- **The database is shared with the bot and the dashboard.** DuckDB lets one process write at
  a time. If the bot is in the middle of a command, `sitt-collect` retries for about 15
  seconds, then gives up on loading for that run. Nothing is lost: the Parquet batch stays in
  `data/observations/` and the next run loads it.
- **The PC must be on.** Runs missed while it is off or asleep are simply gaps.
- **Don't run this and the GitHub schedule together** against the same source: that would
  double the polling rate.
- Raw responses pile up under `data/raw/` (about 30 KB per NTES run). Delete old days when
  they are no longer useful.

## 7. The run log

Every `sitt-collect` run writes one row per source to the `collector_runs` table, whether or
not anything was fetched:

| Column | Meaning |
| --- | --- |
| `run_id` | The batch ID, e.g. `20261005T144504Z-a0f215`. Equal to `observations.batch_id` for the run's readings |
| `source` | `ntes` or `mobond` |
| `started_at`, `finished_at` | When the run started and when fetching ended |
| `status` | `ok`: fetched, parsed and loaded. `partial`: fetched, but nothing was parsed, or the readings couldn't be loaded into the database yet. `failed`: the source couldn't be fetched or parsed. `skipped_disabled`: the source is switched off (Mobond, by default) |
| `readings` | Rows the source produced |
| `trains_matched`, `trains_unmatched` | Distinct train numbers in the run that are, or aren't, in the timetable |
| `error` | One short line about what went wrong. Query strings are removed and it is never a response body |
| `host` | The machine that ran it |
| `backfilled` | True for rows rebuilt from `observations` for runs made before this table existed. Their start time is the one in the batch ID |

Two things follow from this:

- **No row means no run.** The machine was off or asleep, or Task Scheduler didn't start the
  task. A `failed` row means the run happened and the source didn't answer.
- **Writing the log never stops collection.** If the database is busy, the rows are appended
  to `data/logs/collector_runs.pending.jsonl` and moved into the table by the next run that
  can open the database.

## 8. The health report

```bash
uv run sitt-health
```

It prints, for the last 24 hours:

```
Collector health, 05 Oct 21:00 to 06 Oct 21:00 IST (24 h)
Runs: 66 recorded, about 96 expected at one every 15 min
Gaps: 1, about 29 run(s) missed. No run was recorded in these stretches (machine off or asleep, or the task didn't start):
  05 Oct 23:30 to 06 Oct 07:00: 7 h 30 min, about 29 run(s)
Sources:
  mobond: DISABLED (switched off in 66 run(s))
  ntes: UP; runs ok 64, partial 0, failed 2; 6,120 readings; trains matched 71, unmatched 38
    failed 06 Oct 10:15 to 10:30 (2 run(s)): POST https://enquiry.indianrail.gov.in/mntes/q returned HTTP 503
Newest observation: 06 Oct 20:45 IST (15 min ago)
Data quality:
  ...
```

| Option | Meaning |
| --- | --- |
| `--hours N` | Look back `N` hours instead of 24 |
| `--date YYYY-MM-DD` | Report on one Mumbai calendar day (today so far, if it isn't over) |
| `--db PATH` | A database other than `SITT_DB_PATH` |
| `--telegram` | Also send a compact summary to the users in `ALLOWED_USER_IDS` |
| `--alert-only` | Send a message only if something is wrong, or has just recovered |
| `--dry-run` | Print what would be sent. Send nothing and store nothing |

All times are Asia/Kolkata. The report opens the database read-only. If the collector has the
file at that moment it waits and retries a few times, then says the database is busy and
exits with code 2.

How to read it:

- **Runs** counts distinct runs against the number the cadence predicts. The time before the
  collector's first-ever run isn't counted.
- **Gaps** are stretches longer than the cadence plus a tolerance (15 + 10 minutes) with no
  run at all.
- A source is **UP** if its most recent run answered and **DOWN** if it failed. Each run of
  consecutive failures is listed with the last error.
- **Trains matched / unmatched** counts distinct train numbers seen in the window. NTES lists
  long-distance trains at Kalyan, which aren't in the suburban timetable, so a steady number
  of unmatched trains is normal. See [`data-quality.md`](data-quality.md) for which they are.
- **Data quality** is the headline from `sitt-dq` ([`data-quality.md`](data-quality.md)).

### Sending it to Telegram

`sitt-health` talks to the Bot API directly, so the polling bot doesn't need to be running.
It uses `TELEGRAM_BOT_TOKEN` and `ALLOWED_USER_IDS` from `.env`
([`bot-setup.md`](bot-setup.md)).

**Nothing is really sent until `.env` has `SITT_TELEGRAM_SEND=true`.** Without it, `--telegram`
and `--alert-only` print the message under a `[dry run]` line. Try that first:

```bash
uv run sitt-health --telegram --dry-run
```

Then add `SITT_TELEGRAM_SEND=true` to `.env` and run `uv run sitt-health --telegram` once by
hand to see the message arrive.

### Alerts

```bash
uv run sitt-health --alert-only
```

This checks three things and sends one message if any is true:

| Alert | Fires when | Setting (default) |
| --- | --- | --- |
| No successful run | No source has had an `ok` run for this long | `SITT_ALERT_NO_RUN_HOURS` (3) |
| Source down | A source failed in this many runs in a row | `SITT_ALERT_SOURCE_DOWN_RUNS` (4) |
| Readings dropped | A source's mean readings per run, over its last `SITT_ALERT_READINGS_RUNS` (4) answered runs, is below this | `SITT_ALERT_MIN_READINGS` (10) |

To avoid repeating itself, it remembers what it said in the `alert_state` table:

- a new problem is sent once, as `ALERT:`;
- a problem that is still there is repeated as `STILL:` only every `SITT_ALERT_REPEAT_HOURS`
  (12);
- when it goes away, one `RECOVERED:` message is sent.

A dry run stores nothing, so the first real run still sends. If sending fails, nothing is
stored and the next check tries again.

The alert check can't tell you the machine is asleep, because it is asleep too. The first
check after it wakes reports the missed hours if they exceed `SITT_ALERT_NO_RUN_HOURS`; if the
collector's catch-up run finishes first, it says nothing. The daily summary shows the gap
either way.

Other settings, all optional:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SITT_COLLECT_CADENCE_MINUTES` | 15 | How often the collector is scheduled |
| `SITT_HEALTH_GAP_TOLERANCE_MINUTES` | 10 | Lateness allowed before two runs count as having a gap between them |
| `SITT_TELEGRAM_SEND` | false | Really send Telegram messages |

### Scheduling the summary and the alert check

[`scripts/health-check.ps1`](../scripts/health-check.ps1) runs `sitt-health` from the
repository folder and appends what it printed to `data/logs/health.log`. Register two tasks
with it. Replace the path if the repository is somewhere else, and keep the inner `\"` quotes
if the path contains spaces.

A daily summary at 21:00 (the PC's clock, which should be IST):

```powershell
schtasks /Create /TN "SITT health summary" /SC DAILY /ST 21:00 /F /TR "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"C:\Users\vinay\Documents\should_i_take_this_train\scripts\health-check.ps1\" -Mode summary"
```

An alert check every hour, at five past:

```powershell
schtasks /Create /TN "SITT health alerts" /SC HOURLY /MO 1 /ST 00:05 /F /TR "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"C:\Users\vinay\Documents\should_i_take_this_train\scripts\health-check.ps1\" -Mode alert"
```

Both run under your account while you are signed in and need no administrator rights. Test
one without waiting:

```powershell
schtasks /Run /TN "SITT health alerts"
```

```powershell
Get-Content data\logs\health.log -Tail 20
```

Remove them with:

```powershell
schtasks /Delete /TN "SITT health summary" /F
```

```powershell
schtasks /Delete /TN "SITT health alerts" /F
```

## 9. Tests

```bash
uv run pytest tests/live
```

The tests never touch the network. They run the parsers over the files in
[`tests/fixtures/live/`](../tests/fixtures/live/), and run the whole collector against a fake
HTTP transport that serves those files. The NTES file is a trimmed copy of a real board page
saved on 2026-09-27. The Mobond file is invented: it has the feed's structure and every status
wording the parser handles, but none of Mobond's data.
