"""Backtest: does the state of the line predict a train's delay better than its own reading?

    uv run python -m sitt.models.line_state
    uv run python -m sitt.models.line_state --by-direction

For every suburban run with a final reading (its last reading taken within `CLOSE_MINUTES`
of the train's time, as in sitt.models.live_drift), pretend to ask `lead` minutes before
that time, using only readings taken by then, and predict the final delay four ways:

    zero   the timetable: 0 minutes late
    own    the run's own latest reading by then (0 if none)
    line   the median delay of *other* trains in the same direction read in the previous
           `LINE_WINDOW_MINUTES` within `CLOSE_MINUTES` of their own time (readings close to
           a train are the ones that are informative; see docs/live-drift.md); 0 if none
    max    the larger of own and line

No parameters are fitted, so there is nothing to overfit; the result can still be noise on
a small sample. See docs/line-state.md.
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb

from sitt.models.live_drift import CLOSE_MINUTES

LINE_WINDOW_MINUTES = 30
LEADS = (15, 30, 45, 60)
PREDICTORS = ("zero", "own", "line", "max")


@dataclass(frozen=True)
class BacktestRow:
    lead: int
    direction: str | None  # None: both directions together
    runs: int
    with_line: float  # share of runs where some other train gave a line state
    mae: dict[str, float]  # mean absolute error, by predictor
    within5: dict[str, float]  # share within 5 minutes, by predictor


def _prepare(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE ls_readings AS
        SELECT o.train_id, t.direction, o.event, o.observed_at,
               o.actual_or_expected_time AS t, o.delay_minutes AS d,
               date_trunc('day', o.actual_or_expected_time
                   - to_minutes(CAST(o.delay_minutes AS BIGINT)) - INTERVAL 3 HOUR) AS run_key
        FROM observations o JOIN trains t USING (train_id)
        WHERE o.delay_minutes IS NOT NULL AND o.actual_or_expected_time IS NOT NULL
          AND NOT coalesce(o.cancelled, false) AND o.event IN ('arrival', 'departure')
          AND o.source <> 'synthetic'
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE ls_final AS
        SELECT train_id, direction, event, run_key,
               arg_max(d, observed_at) AS d_final, arg_max(t, observed_at) AS t_final
        FROM ls_readings GROUP BY ALL
        HAVING max(observed_at) >= arg_max(t, observed_at) - INTERVAL {CLOSE_MINUTES} MINUTE
        """
    )


def backtest(
    con: duckdb.DuckDBPyConnection, leads=LEADS, by_direction: bool = False
) -> list[BacktestRow]:
    _prepare(con)
    group = "direction" if by_direction else "NULL::VARCHAR"
    out: list[BacktestRow] = []
    for lead in leads:
        rows = con.execute(
            f"""
            WITH q AS (
                SELECT *, t_final - INTERVAL {int(lead)} MINUTE AS t_ask FROM ls_final
            ), own AS (
                SELECT q.train_id, q.event, q.run_key, arg_max(r.d, r.observed_at) AS d_own
                FROM q JOIN ls_readings r USING (train_id, event, run_key)
                WHERE r.observed_at <= q.t_ask GROUP BY ALL
            ), line AS (
                SELECT q.train_id, q.event, q.run_key, median(h.d) AS d_line
                FROM q JOIN ls_readings h
                  ON h.direction = q.direction AND h.train_id <> q.train_id
                 AND h.observed_at BETWEEN q.t_ask - INTERVAL {LINE_WINDOW_MINUTES} MINUTE
                                       AND q.t_ask
                 AND h.observed_at >= h.t - INTERVAL {CLOSE_MINUTES} MINUTE
                GROUP BY ALL
            ), j AS (
                SELECT q.direction, q.d_final, coalesce(own.d_own, 0) AS own,
                       coalesce(line.d_line, 0) AS line, line.d_line IS NOT NULL AS has_line
                FROM q LEFT JOIN own USING (train_id, event, run_key)
                       LEFT JOIN line USING (train_id, event, run_key)
            )
            SELECT {group} AS grp, count(*), avg(has_line::INT),
                   avg(abs(d_final)), avg(abs(d_final - own)), avg(abs(d_final - line)),
                   avg(abs(d_final - greatest(own, line))),
                   avg((abs(d_final) <= 5)::INT), avg((abs(d_final - own) <= 5)::INT),
                   avg((abs(d_final - line) <= 5)::INT),
                   avg((abs(d_final - greatest(own, line)) <= 5)::INT)
            FROM j GROUP BY grp ORDER BY grp
            """
        ).fetchall()
        for grp, runs, with_line, *errors in rows:
            out.append(
                BacktestRow(
                    lead=lead,
                    direction=grp,
                    runs=int(runs),
                    with_line=float(with_line),
                    mae=dict(zip(PREDICTORS, map(float, errors[:4]), strict=True)),
                    within5=dict(zip(PREDICTORS, map(float, errors[4:]), strict=True)),
                )
            )
    return out


def format_table(rows: list[BacktestRow]) -> str:
    by_direction = any(row.direction for row in rows)
    head = (
        "| Asked before due |" + (" Direction |" if by_direction else "") + " Runs | Line known |"
    )
    head += "".join(f" MAE {p} |" for p in PREDICTORS) + " ≤5 own | ≤5 max |"
    lines = [head, "|" + " --- |" * (head.count("|") - 1)]
    for row in rows:
        cells = [f"{row.lead} min"] + ([row.direction] if by_direction else [])
        cells += [str(row.runs), f"{row.with_line:.0%}"]
        best = min(row.mae.values())
        cells += [
            f"**{row.mae[p]:.1f}**" if row.mae[p] == best else f"{row.mae[p]:.1f}"
            for p in PREDICTORS
        ]
        cells += [f"{row.within5['own']:.0%}", f"{row.within5['max']:.0%}"]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from sitt.config import load_settings

    parser = argparse.ArgumentParser(
        prog="python -m sitt.models.line_state",
        description="Backtest the line-state nowcast against a train's own live reading.",
    )
    parser.add_argument("--db", type=Path, help="database (default SITT_DB_PATH)")
    parser.add_argument("--by-direction", action="store_true", help="split by direction")
    args = parser.parse_args(argv)
    db_path = args.db or load_settings().db_path
    if not Path(db_path).exists():
        print(f"No database at {db_path}.", file=sys.stderr)
        return 2
    with duckdb.connect(str(db_path), read_only=True) as con:
        rows = backtest(con, by_direction=args.by_direction)
    if not rows:
        print("No runs to backtest yet. Collect a few rounds first.")
        return 1
    print(format_table(rows))
    print()
    print("MAE: mean absolute error in minutes (lowest in bold). ≤5: share within 5 minutes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
