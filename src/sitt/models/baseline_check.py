"""Which "typical past delay" predicts better: every reading, or each run once?

    uv run python -m sitt.models.baseline_check [--db PATH]

The recommender's baseline (sitt.recommend._baseline_predictions) is the median of every
reading of a train at a station on earlier days. A train is often read several times as it
approaches, and early readings tend to say "on time" for trains that end up late
(docs/live-drift.md), so that median may be too low. The alternative counts each run once,
with the delay of its last reading taken close to the train's time (the same rule as Delay
patterns: sitt.models.live_drift.runs_ctes).

This backtests both on the real readings. Each run's final delay is predicted from earlier
days only, both ways, and the mean absolute error and the mean of (actual - predicted) are
compared, overall and by direction, over the runs both ways can predict. It also says
whether there are enough runs (`MIN_RUNS`) to act on. Nothing is changed by running it.
"""

import argparse
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb

from sitt.models.live_drift import runs_ctes

# Fewer comparable runs than this, and the result is not a reason to change the baseline.
MIN_RUNS = 200


@dataclass(frozen=True)
class Row:
    group: str  # "all", "up" or "down"
    runs: int
    mae_every_reading: float
    mae_each_run: float
    bias_every_reading: float  # mean of actual - predicted: positive means too optimistic
    bias_each_run: float


def backtest(con: duckdb.DuckDBPyConnection) -> list[Row]:
    # "Every reading" is exactly what the recommender's baseline reads: the `obs` table of
    # sitt.models.features (matched to the train's route, no flagged or cancelled readings),
    # by its service day.
    from sitt.models import features

    features.prepare(con)
    rows = con.execute(
        f"""
        WITH {runs_ctes(suburban_only=True)},
        fin AS (
            SELECT train_id, station_code, CAST(run_key AS DATE) AS day, d_final FROM final
        )
        SELECT f.d_final, t.direction,
               (SELECT median(o.delay_minutes) FROM obs o WHERE o.train_id = f.train_id
                  AND o.station_code = f.station_code AND o.service_day < f.day
                  AND o.source <> 'synthetic'),
               (SELECT median(g.d_final) FROM fin g WHERE g.train_id = f.train_id
                  AND g.station_code = f.station_code AND g.day < f.day),
               f.train_id, f.day
        FROM fin f JOIN trains t USING (train_id)
        """
    ).fetchall()
    both = [r for r in rows if r[2] is not None and r[3] is not None]
    out = []
    for group in ("all", "up", "down"):
        sel = [r for r in both if group == "all" or r[1] == group]
        if not sel:
            continue
        out.append(
            Row(
                group,
                # Runs: one train on one day, however many stations it was read at.
                len({(r[4], r[5]) for r in sel}),
                statistics.mean(abs(r[0] - r[2]) for r in sel),
                statistics.mean(abs(r[0] - r[3]) for r in sel),
                statistics.mean(r[0] - r[2] for r in sel),
                statistics.mean(r[0] - r[3] for r in sel),
            )
        )
    return out


def report(rows: list[Row]) -> str:
    if not rows:
        return "No run has an earlier day to predict it from yet: nothing to compare."
    names = {"all": "All trains", "up": "Towards CSMT", "down": "Away from CSMT"}
    lines = [
        "| Trains | Runs | Error, every reading | Error, each run once "
        "| Too optimistic by, every reading | each run once |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        lines.append(
            f"| {names[r.group]} | {r.runs} | {r.mae_every_reading:.1f} min "
            f"| {r.mae_each_run:.1f} min | {r.bias_every_reading:+.1f} min "
            f"| {r.bias_each_run:+.1f} min |"
        )
    total = rows[0]
    better = "each run once" if total.mae_each_run < total.mae_every_reading else "every reading"
    if total.runs < MIN_RUNS:
        verdict = (
            f"Only {total.runs} runs can be compared ({MIN_RUNS} needed): not enough to change "
            f"the baseline. For now {better} is ahead."
        )
    else:
        verdict = f"{total.runs} runs: {better} predicts better overall."
    return "\n".join(["", *lines, "", verdict])


def main(argv: list[str] | None = None) -> int:
    from sitt.config import load_settings

    parser = argparse.ArgumentParser(
        prog="python -m sitt.models.baseline_check",
        description="Backtest the typical-past-delay baseline: every reading vs each run once.",
    )
    parser.add_argument("--db", type=Path, help="database (default SITT_DB_PATH)")
    args = parser.parse_args(argv)
    db = args.db or load_settings().db_path
    if not Path(db).exists():
        print(f"No database at {db}.")
        return 2
    with duckdb.connect(str(db), read_only=True) as con:
        print(report(backtest(con)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
