# Experiment: long-distance trains as a congestion signal

**Status: an experiment that is wired in and switched off. Nothing here shows it helps.**

## The idea

NTES's board for Kalyan lists mail and express trains as well as locals, each with a live
delay. Those trains share the fast lines with the fast locals. If the expresses through
Kalyan are running late, the corridor is probably congested, and that might say something
about how late a local will be that the locals' own readings don't.

## What was built

Two extra inputs to the delay model, in
[`src/sitt/models/features.py`](../src/sitt/models/features.py):

| Feature | Meaning |
| --- | --- |
| `ld_median_delay` | The median delay, in minutes, of long-distance trains in the last collector batch before the prediction is made |
| `ld_count` | How many long-distance trains that was |

- A **long-distance train** is any train in a batch that isn't in the suburban timetable.
  NTES lists 20 to 30 of them at Kalyan in a two-hour board.
- A train's arrival and departure rows are averaged first, so it counts once.
- Only the **last batch before the cutoff** is used, and only if it is no more than 20
  minutes old: the same rule as the existing "state of the line" features. Nothing observed
  after the moment of prediction can reach the row. Tests check this by changing later
  batches and confirming earlier rows don't move.
- Cancelled readings, readings with no delay, and readings flagged by `sitt-dq` are left out.
- The two columns are computed for every row, but **a model only uses them if it was trained
  with them**. A model records its own list of inputs, so models with and without the
  feature can coexist.

## Switching it on

```
SITT_FEATURE_LONG_DISTANCE=true
```

With that in `.env`, `sitt-retrain` and `python -m sitt.models train` train with the two
extra inputs. It is off by default. `python -m sitt.models train --long-distance-feature`
does the same for one run.

## Trying it on synthetic data (plumbing only)

The standard synthetic dataset has no long-distance trains. The generator can invent some,
at Kalyan, in the shape of an NTES board:

```bash
uv run python -m sitt.synth --weeks 5 --long-distance --db scratch/synth-ld.duckdb --out scratch/synth-ld
```

```bash
uv run python -m sitt.models ablate --db scratch/synth-ld.duckdb --test-weeks 1 --valid-weeks 1 --max-train-rows 300000
```

`ablate` trains the model twice on the same data, split and seed, with and without the
feature, and prints both. Run on 2026-10-05:

```
SYNTHETIC RESULTS: THESE SAY NOTHING ABOUT REAL TRAINS
Feature group: long_distance (ld_median_delay, ld_count)
Data: scratch/synth-ld.duckdb; tested on 2026-06-29 to 2026-07-05 (115,970 rows; 300,000 trained on)
Rows where the feature has a value: 587,069

                           MAE  within 2  within 5  coverage   width
without the feature      1.220     89.4%     99.1%     77.1%    3.75
with the feature         1.218     89.5%     99.1%     76.5%    3.68

MAE change with the feature: -0.002 min. Share of the model's gain: ld_median_delay 0.6%, ld_count 0.0%.
```

What this shows, and all it shows:

- **The plumbing works.** The feature is computed, has a value in most rows, reaches the
  model, and the model gives it a little weight.
- **It says nothing about whether the idea is right.** The invented long-distance trains
  are late by an amount tied to the same invented "state of the line" that the locals
  follow, so any signal in them was put there by the generator, and the existing
  `line_median_delay` feature already carries it. A change of 0.002 minutes is noise.

The invented long-distance trains are off by default, so the standard synthetic dataset and
the results on [`model-results.md`](model-results.md) are unchanged. Switching them on uses
a separate random stream and changes no other reading.

## How it will be judged on real data

Every time `sitt-retrain` trains a model ([`retraining.md`](retraining.md)), it trains a
second one with the feature switched the other way, on the same time split, and records both
sets of test results in the manifest and in the "Real data so far" section of
`model-results.md`, with the number of rows where the feature had a value.

Which of the two is saved and considered for promotion is decided by
`SITT_FEATURE_LONG_DISTANCE`, not by which did better. One split is not enough to pick a
feature by. Look at several weeks of those comparisons before turning it on.

Things that could make it useless on real data:

- NTES covers locals beyond Kalyan, where long-distance trains are few. The locals it would
  help most (CSMT–Kalyan fast trains) aren't in NTES at all.
- A long-distance train's delay at Kalyan was mostly picked up hundreds of kilometres away.
  A train six hours late says little about the next half hour at Kalyan.
- The median of 20 to 30 unrelated trains may just be noise.
