# Delay model results (SYNTHETIC RESULTS: these say nothing about real trains)

**The model below was trained and tested on invented data** from `sitt.synth`
(see [`synthetic-data.md`](synthetic-data.md)). The numbers show that the
pipeline runs end to end and that the model can find patterns the generator put
there. They are **not** an estimate of how well any of this predicts real trains.
Real accuracy can only be measured once real observations have been collected.

Generated from `data/synthetic.duckdb` on 2026-10-05 by `python -m sitt.models train`.

## Data and split

The split is by time only: the model never trains on days after the ones it is
tested on, and rows are never shuffled between periods.

| Period | Days | Rows |
| --- | --- | --- |
| Train | 2026-06-01 to 2026-08-09 | 1,168,408 |
| Validation | 2026-08-10 to 2026-08-23 | 233,482 |
| Test | 2026-08-24 to 2026-09-20 | 462,091 |

Observation sources: `synthetic`.
A row is one question: *given what was known at some earlier moment, how late was
this train at this station?* One observation gives several rows: one per earlier
reading of the same trip, and one from before the train started. See
`src/sitt/models/features.py`.

## Accuracy on the test period

MAE is the mean absolute error in minutes. "Within 2 min" is the share of
predictions that, rounded to a whole minute, are no more than 2 minutes from the
delay that was observed. (Sources report delays in whole minutes.)

### All test rows

462,091 rows.

| Predictor | MAE (min) | Within 2 min | Within 5 min |
| --- | --- | --- | --- |
| LightGBM model | 1.44 | 87.3% | 96.8% |
| Baseline: median by train and station | 1.81 | 85.1% | 94.4% |
| Baseline: median by hour, weekday and direction | 2.73 | 67.0% | 88.4% |
| Baseline: always on time | 4.00 | 45.1% | 77.7% |

### Before the train has been seen (no live reading yet)

128,713 rows.

| Predictor | MAE (min) | Within 2 min | Within 5 min |
| --- | --- | --- | --- |
| LightGBM model | 1.55 | 86.9% | 96.2% |
| Baseline: median by train and station | 1.60 | 87.1% | 95.7% |
| Baseline: median by hour, weekday and direction | 2.43 | 70.0% | 91.3% |
| Baseline: always on time | 3.32 | 54.4% | 83.2% |

### With an earlier live reading of the same trip

333,378 rows.

| Predictor | MAE (min) | Within 2 min | Within 5 min |
| --- | --- | --- | --- |
| LightGBM model | 1.40 | 87.5% | 97.1% |
| Baseline: median by train and station | 1.89 | 84.3% | 93.9% |
| Baseline: median by hour, weekday and direction | 2.84 | 65.8% | 87.3% |
| Baseline: always on time | 4.27 | 41.6% | 75.5% |

## Calibration of the 10th–90th percentile range

A well-calibrated range holds the observed delay 80% of the time, with 10% below
and 10% above.

| Rows | Inside the range | Below | Above | Mean width (min) |
| --- | --- | --- | --- | --- |
| All test rows | 78.2% | 10.5% | 11.2% | 4.1 |
| Before the train has been seen (no live reading yet) | 78.8% | 10.2% | 11.0% | 4.3 |
| With an earlier live reading of the same trip | 78.0% | 10.7% | 11.3% | 4.1 |

## What the model leans on

Share of the median model's total split gain, top ten features.

| Feature | Share of gain |
| --- | --- |
| `hist_delay` | 59.3% |
| `prior_delay` | 13.4% |
| `train_id` | 10.7% |
| `line_median_delay` | 5.6% |
| `lead_minutes` | 4.2% |
| `station_code` | 1.6% |
| `prior_lead_minutes` | 1.3% |
| `prior_points_back` | 1.2% |
| `hist_count` | 0.6% |
| `month` | 0.4% |

## Settings

- Trees after early stopping: median 185, 10th percentile 201, 90th percentile 46.
- Seed 1; LightGBM parameters are in `src/sitt/models/delay.py`.

## How to read this

- The model beating the baselines here means the features carry the signal the
  generator planted (delay building along a route, the state of the line, peaks,
  megablocks). It does not mean real delays follow those patterns.
- The synthetic delays are smoother and more regular than real ones are likely to
  be, so real errors will probably be larger.
- When real observations exist, run the same command on the real database and
  replace this page.
