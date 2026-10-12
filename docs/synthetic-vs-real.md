# Synthetic assumptions against the first real readings

The synthetic generator ([`synthetic-data.md`](synthetic-data.md)) was written before any
real delay had been seen. This page compares it with the first real readings. Reproduce it
with:

```bash
uv run python -m sitt.synth --weeks 4 --start 2026-10-05 --db data/synthetic-oct.duckdb --out data/synthetic-oct
uv run python -m sitt.synth.compare --synthetic-db data/synthetic-oct.duckdb
```

`sitt.synth.compare` keeps to the stations, kinds of day and hours of day the real data
covers, so the dates don't need to match.

## What was compared

- **Real:** NTES readings at Kalyan, Monday 5 October 20:00–22:45 and Tuesday 6 October
  09:30–12:45, 2026. One value per suburban run: its last reading taken within 15 minutes of
  its time (as in [`live-drift.md`](live-drift.md)). 134 runs.
- **Synthetic:** four weeks from 5 October (no monsoon, one megablock), real timetable,
  default settings, at Kalyan and the stations either side of it, weekdays, the same hours.

## Result

Stations: KYN. Hours: 09–12 and 19–22, weekdays.

| Direction | Type | Data | Runs | Mean | Median | 90th pct | On time (≤1) | 5+ late | 10+ late |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| down | fast | real | 51 | 2.5 | 0 | 0 | 92% | 8% | 8% |
| down | fast | synthetic | 973 | 3.6 | 3 | 8 | 29% | 28% | 7% |
| down | slow | real | 20 | 1.8 | 0 | 0 | 95% | 5% | 5% |
| down | slow | synthetic | 829 | 2.7 | 2 | 6 | 36% | 17% | 2% |
| up | fast | real | 32 | 11.3 | 11 | 22 | 28% | 69% | 59% |
| up | fast | synthetic | 480 | 1.6 | 1 | 4 | 57% | 8% | 1% |
| up | slow | real | 31 | 11.8 | 12 | 24 | 32% | 65% | 58% |
| up | slow | synthetic | 1158 | 0.8 | 1 | 3 | 74% | 2% | 0% |

The pattern held in both sessions separately: up trains had a median of 10 min late on the
Monday evening and 13 on the Tuesday morning; down trains were on time 89% and 94% of the
time.

## What it says

1. **The direction is the wrong way round.** The generator builds delay up from each train's
   first station, so a train towards CSMT is barely late at Kalyan (it has just started) and
   a train away from CSMT is late there (it has come all the way from CSMT). In the real
   readings, trains **towards** CSMT were already 11–12 minutes late at Kalyan, and trains
   away from it were almost all on time.
2. **Real delays are lumpier.** Synthetic delays are small and spread smoothly (90th
   percentile 3–8 min). Real ones are mostly 0 or 10+, with little in between.
3. **Real "on time" is very on time.** 92–95% of down trains were within a minute. Part of
   that may be NTES itself: it shows trains as on time until they are close
   ([`live-drift.md`](live-drift.md)), and the "final" reading can still be up to 15 minutes
   before the train.

## Why not change the generator yet

- **Two sessions on two days.** The up-train delays could be one bad evening and one bad
  morning on the Kasara and Karjat branches. Retuning to that would be fitting noise.
- **NTES shows a biased slice.** At Kalyan it lists trains going beyond Kalyan: up trains
  from Kasara, Karjat, Badlapur and Ambernath, which have already run a long single route
  shared with long-distance trains. The CSMT–Kalyan locals that start at Kalyan, the core of
  this project, are not in the real data at all.
- The synthetic data's job is to exercise the pipeline, and it still does. No model trained
  on it is used for recommendations.

## What to change once there is more data

If a few more weeks of readings (collected by hand) show the same thing:

- Give up trains from the outer branches a **starting delay** that depends on the branch
  (Kasara/Karjat side vs. starting at Kalyan), rather than the same ~1 minute everywhere
  (`origin_delay_mean` in `SynthConfig`).
- Make delays **two-state**: most runs on time, a minority 10+ minutes late, instead of a
  smooth spread.
- Re-run this comparison, and [`live-drift.md`](live-drift.md), with the new data before
  deciding.
