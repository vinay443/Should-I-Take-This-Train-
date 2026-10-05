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

1. On GitHub, open **Actions → Probe live sources → Run workflow**.
2. Leave `sources` as `mobond ntes`, or enter just `ntes` to leave Mobond alone.
3. Read the **Probe** step's log. For each source it prints every request with its status code
   and size, the content type, a 400-character sample of the response, and how many
   observations were parsed.

The probe makes one fetch per source and stores nothing. The job fails if a source can't be
fetched or parsed. The same check runs locally with
`uv run python -m sitt.ingest.live.probe ntes`.

If NTES is blocked from the runners, the options are in the "Fallback" section of
[`data-sources.md`](data-sources.md): the simplest is to run the collector from a machine with
an Indian IP instead.

## 4. Turning on the schedule

The **Collect live data** workflow ([`collect.yml`](../.github/workflows/collect.yml)) can
only be started by hand until you do the following.

1. **Get Mobond's agreement**, or decide to collect NTES only.
2. **Run the probe** (above) and confirm the sources you want answer from a runner.
3. **Run the collector once by hand:** Actions → Collect live data → Run workflow. The first
   run creates the `data` branch. Check that it contains a Parquet file.
4. **Switch Mobond on, if agreed:** Settings → Secrets and variables → Actions → Variables →
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
- This repository is public, so the `data` branch is too. See the next section.

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

**Raw responses** are not committed by default. On GitHub they are kept as a workflow artifact
for 90 days and then deleted. Setting the repository variable `SITT_COMMIT_RAW=true` commits
them to the `data` branch instead. Don't do that while the repository is public: it would
republish Mobond's feed.

Note that the Parquet files already carry Mobond's status text for each train, in the
`raw_status` column. On a public repository that is close to republishing the feed, which
[`data-sources.md`](data-sources.md) advises against. Raise it when asking Mobond for
permission, or make the repository private before switching Mobond on.

## 6. Tests

```bash
uv run pytest tests/live
```

The tests never touch the network. They run the parsers over trimmed copies of real responses
saved on 2026-09-27 in [`tests/fixtures/live/`](../tests/fixtures/live/), and run the whole
collector against a fake HTTP transport that serves those files.
