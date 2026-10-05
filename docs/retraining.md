# Training on real data

Real observations only started on 5 October 2026, so there is nowhere near enough to train a
model on. `sitt-retrain` is the pipeline that will do it when there is. Until then it reports
how far along the data is and trains nothing. It lives in
[`src/sitt/models/retrain.py`](../src/sitt/models/retrain.py).

**It is not a real model today.** The results on [`model-results.md`](model-results.md) are
synthetic, apart from the one section this command writes.

```bash
uv run sitt-retrain
```

| Option | Meaning |
| --- | --- |
| `--check` | Check the gates and update the docs page. Never train |
| `--db PATH` | A database other than `SITT_DB_PATH` |
| `--models-dir PATH` | Where versioned models are saved. Default: the folder holding `SITT_MODEL_DIR`, i.e. `models/` |
| `--doc PATH` | The results page whose real-data section to rewrite. Default `docs/model-results.md` |
| `--no-doc` | Leave the results page alone |

## What one run does

1. **Recomputes the data-quality flags** ([`data-quality.md`](data-quality.md)), so suspect
   readings are left out of everything that follows.
2. **Checks the readiness gates.** If any isn't met it prints where each stands and exits
   with code 0:

   ```
   Not enough real data yet: days with observations: 1 of 28; usable observations: 326 of 5,000; observations in the last 7 days (the test window): 326 of 500.
     [ ] days with observations: 1 of 28
     [ ] usable observations: 326 of 5,000
     [x] observations at the best-covered station: 326 of 300
     [ ] observations in the last 7 days (the test window): 326 of 500
   ```

3. **Trains**, if every gate is met: the same three LightGBM models as the synthetic
   pipeline (the likely delay, and the 10th and 90th percentiles), on real observations only.
   The split is by time: the last week is the test set, the week before it stops training
   early, and everything earlier is trained on.
4. **Evaluates** against the three baselines (always on time; median by train and station;
   median by hour, weekday and direction): MAE, the share within 2 and 5 minutes, how much
   of the test set the 10th–90th percentile range holds, pinball loss for each end of the
   range, and the sample size and error at every station.
   It also trains the model a second time with the
   [long-distance feature](long-distance-feature.md) switched the other way, and records
   both results, so that experiment is judged on real data as it arrives.
5. **Saves a versioned model** under `models/<version>/`, e.g.
   `models/real-20261102T033000Z/`, with a `manifest.json`. `models/` is gitignored.
6. **Decides whether to promote it** (below) and records the outcome in the
   `model_registry` table.
7. **Rewrites the "Real data so far" section** of [`model-results.md`](model-results.md).

The real database is opened only briefly, to refresh the flags and to write the registry
row. Training runs on a private copy in a temporary folder, so the collector is never kept
waiting. If the database is busy when it is needed, the command says so and exits with code 2.

A database that holds any synthetic observation is refused outright.

## Readiness gates

A "usable observation" is a real reading with a delay, for a train in the timetable, at a
station on its route, not cancelled and not flagged.

| Gate | Default | Variable |
| --- | --- | --- |
| Distinct days with usable observations | 28 | `SITT_RETRAIN_MIN_DAYS` |
| Usable observations in total | 5,000 | `SITT_RETRAIN_MIN_OBSERVATIONS` |
| Usable observations at the best-covered station | 300 | `SITT_RETRAIN_MIN_STATION_OBSERVATIONS` |
| Usable observations in the test window | 500 | `SITT_RETRAIN_MIN_TEST_OBSERVATIONS` |
| Weeks held out for testing | 1 | `SITT_RETRAIN_TEST_WEEKS` |
| Weeks held out to stop training early | 1 | `SITT_RETRAIN_VALID_WEEKS` |

These are guesses. With a week of test data the figures will still move a lot from run to
run, and the results page says so whenever the test set is under 2,000 rows.

## Promotion

A newly trained model replaces the one in use only if **both** hold on the test period:

1. Its MAE is lower than the **best** of the three baselines, by more than
   `SITT_PROMOTE_MIN_MAE_GAIN` minutes (default 0, i.e. strictly lower).
2. Its 10th–90th percentile range holds between `SITT_PROMOTE_COVERAGE_MIN` (0.70) and
   `SITT_PROMOTE_COVERAGE_MAX` (0.90) of the test rows. A perfect range holds 80%.

Promoted: the model's files are copied to `SITT_MODEL_DIR` (`models/delay`), which is where
`/next` and the dashboard load it from, and the previously promoted version is marked
`superseded`. If a synthetic model is sitting in that folder it is moved to
`models/delay-synthetic` rather than deleted.

Not promoted: the version is saved and recorded as `rejected`, with the reason, and whatever
was in use stays in use. If nothing was, `/next` keeps using past delays or the timetable.

A model trained on synthetic data is never promoted by this command.

## The manifest and the registry

`models/<version>/manifest.json` records: `version`, `created_at`, `git_commit`,
`is_synthetic` (always `false`), the observation sources, the data window and split, row
counts (observations, days, flagged and left out, feature rows per period, observations per
station), the feature list, the stations covered, the model's metrics overall, by segment
and by station, the baselines' metrics, the gates, and the promotion decision with the rule
it was judged by.

The `model_registry` table keeps one row per version, which survives even if `models/` is
deleted:

```sql
SELECT version, status, model_mae, best_baseline, best_baseline_mae, range_coverage, reason
FROM model_registry ORDER BY created_at DESC;
```

## Coverage: which stations a real model speaks for

NTES, the only live source collected so far, reports locals **beyond Kalyan** (the Titwala,
Kasara, Badlapur and Karjat directions) and long-distance trains. It does not report
CSMT–Kalyan locals. A model trained on that data has seen Kalyan and nothing towards CSMT.

So each real model carries a list of `covered_stations`: those with at least
`SITT_RETRAIN_MIN_STATION_OBSERVATIONS` usable observations **and** rows in both the training
and test periods. The recommender honours it:

- **Both stations covered:** the model is used.
- **One covered:** the model is used there. The other station's time comes from the typical
  past delay, or the timetable, and the reply says "The delay model covers Kalyan only.
  Times at Dadar use the typical past delay."
- **Neither covered:** the model is not used. The reply says a model exists but was not
  trained on these stations, and names the level it actually used.

A model without the list (a synthetic one, or one from `python -m sitt.models train`) is
treated as covering everything, as before.

## Running it weekly

Data is local, so this runs on your PC, not on GitHub.
[`scripts/retrain.ps1`](../scripts/retrain.ps1) runs `sitt-retrain` from the repository
folder and appends what it printed to `data/logs/retrain.log`. Register it for Monday at
03:30. Replace the path if the repository is somewhere else:

```powershell
schtasks /Create /TN "SITT weekly retrain" /SC WEEKLY /D MON /ST 03:30 /F /TR "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"C:\Users\vinay\Documents\should_i_take_this_train\scripts\retrain.ps1\""
```

If the PC is asleep at that time the run is skipped until next week. To catch up after a
missed start, open the task in Task Scheduler and tick **Run task as soon as possible after
a scheduled start is missed** on the Settings tab. Try it once by hand:

```powershell
schtasks /Run /TN "SITT weekly retrain"
```

```powershell
Get-Content data\logs\retrain.log -Tail 20
```

Each run rewrites one section of `docs/model-results.md`, so `git status` will show that
file as changed afterwards. Commit it when you want the page on GitHub to move.

## A flaw this exposed

The three baselines are fitted on one row per observation. They used to pick those rows by
"the train had not been seen yet", which is one row per observation only when a train is
never read before it starts. NTES lists a train up to two hours ahead, so with NTES-shaped
data almost no row qualified, and all three baselines quietly fell back to "always on time".
Any model would then have looked good and been promoted. They are now fitted on the first
row of each (train, station, reading time), which gives the same result on the synthetic data
and the right one on NTES's.
