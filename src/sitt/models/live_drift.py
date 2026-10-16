"""How much a live reading's delay changes before the train actually gets there.

    uv run python -m sitt.models.live_drift            suburban trains, as a Markdown table
    uv run python -m sitt.models.live_drift --all      long-distance trains too

NTES gives each train an "expected" time at a station, refreshed every time the collector
asks. This compares each reading with the **last reading of the same run taken close to
when the train was due** (within `CLOSE_MINUTES` of its expected time), which stands in for
what really happened: a 15-minute snapshot usually misses the moment a train passes, so true
"actual" times are rare.

For every earlier reading it records the *lead* (how long before the train's **scheduled**
time the reading was taken: the only time a rider knows when the reading comes in; negative
once that time has passed) and the *drift* (final delay minus the delay that reading gave).
A run is one train at one station on one service day; arrival and departure readings are the
same run. Drift is summarised by lead bucket, separately for readings that said "on time"
(0 or early) and readings that already showed a delay. See docs/live-drift.md for the
results and what the recommender does with them.
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb

from sitt.tz import mumbai_sql

# A reading's scheduled time as Mumbai wall-clock time: runs are grouped by Mumbai service
# day whatever time zone the computer (and so DuckDB's session) is in.
SCHEDULED_LOCAL = mumbai_sql("actual_or_expected_time - to_minutes(CAST(delay_minutes AS BIGINT))")

# The last reading counts as "what happened" only if taken this close to the train's time.
CLOSE_MINUTES = 15
# Lead buckets, in minutes before the train's scheduled time: after it (< 0), [0, 15),
# [15, 30), [30, 60), [60, ...).
AFTER = -1
BUCKETS = (0, 15, 30, 60)


@dataclass(frozen=True)
class DriftRow:
    said_on_time: bool  # the earlier reading gave 0 minutes late
    lead_from: int  # bucket start, minutes
    lead_to: int | None  # bucket end, or None for the last bucket
    readings: int
    mean_abs_drift: float
    p10: float
    median: float
    p90: float
    share_5_later: float  # final delay at least 5 minutes more than the reading said
    share_earlier: float  # final delay at least 3 minutes less

    @property
    def lead_label(self) -> str:
        if self.lead_from == AFTER:
            return "after its time"
        if self.lead_to is None:
            return f"{self.lead_from}+ min"
        return f"{self.lead_from}-{self.lead_to} min"


def _bucket_sql() -> str:
    cases = " ".join(
        f"WHEN lead < {upper} THEN {lower}"
        for lower, upper in zip(BUCKETS, BUCKETS[1:], strict=False)
    )
    return f"CASE WHEN lead < 0 THEN {AFTER} {cases} ELSE {BUCKETS[-1]} END"


def runs_ctes(suburban_only: bool = True) -> str:
    """Two CTEs, `r` (every usable reading) and `final` (one row per run whose last reading
    was taken close to the train's time: its delay `d_final`, `last_seen`, and the run's
    `scheduled` time). Shared with sitt.patterns, so both use the same idea of a run."""
    where_suburban = "AND train_id IN (SELECT train_id FROM trains)" if suburban_only else ""
    return f"""
        r AS (
            SELECT train_id, station_code, observed_at,
                   actual_or_expected_time AS t, delay_minutes AS d,
                   actual_or_expected_time - to_minutes(CAST(delay_minutes AS BIGINT))
                       AS scheduled,
                   -- One run of a train: readings whose scheduled time falls on the same
                   -- service day (shifted 3 h so late-night runs stay together).
                   date_trunc('day', {SCHEDULED_LOCAL} - INTERVAL 3 HOUR) AS run_key
            FROM observations
            WHERE delay_minutes IS NOT NULL AND actual_or_expected_time IS NOT NULL
              AND NOT coalesce(cancelled, false) AND event IN ('arrival', 'departure')
              AND source <> 'synthetic'
              {where_suburban}
        ), final AS (
            SELECT train_id, station_code, run_key,
                   arg_max(d, observed_at) AS d_final, max(observed_at) AS last_seen,
                   arg_max(scheduled, observed_at) AS scheduled
            FROM r GROUP BY ALL
            HAVING max(observed_at) >= arg_max(t, observed_at) - INTERVAL {CLOSE_MINUTES} MINUTE
        )"""


def drift_pairs_sql(suburban_only: bool = True) -> str:
    """One row per (earlier reading, final reading) pair: lead, said_on_time, drift."""
    return f"""
        WITH {runs_ctes(suburban_only)}
        SELECT round(r.d) <= 0 AS said_on_time,
               (epoch(r.scheduled) - epoch(r.observed_at)) / 60 AS lead,
               f.d_final - r.d AS drift
        FROM r JOIN final f USING (train_id, station_code, run_key)
        WHERE r.observed_at < f.last_seen
    """


def drift_table(con: duckdb.DuckDBPyConnection, suburban_only: bool = True) -> list[DriftRow]:
    rows = con.execute(
        f"""
        WITH pairs AS ({drift_pairs_sql(suburban_only)})
        SELECT said_on_time, {_bucket_sql()} AS bucket, count(*),
               avg(abs(drift)), quantile_cont(drift, 0.1), quantile_cont(drift, 0.5),
               quantile_cont(drift, 0.9), avg((drift >= 5)::INT), avg((drift <= -3)::INT)
        FROM pairs GROUP BY ALL ORDER BY said_on_time DESC, bucket
        """
    ).fetchall()
    ends = dict(zip(BUCKETS, [*BUCKETS[1:], None], strict=True)) | {AFTER: 0}
    return [
        DriftRow(bool(on_time), int(b), ends[int(b)], int(n), *(float(v) for v in rest))
        for on_time, b, n, *rest in rows
    ]


def format_table(rows: list[DriftRow]) -> str:
    lines = [
        "| Reading said | Taken before scheduled time | Readings | Mean abs drift | 10th pct "
        "| Median | 90th pct | Ended 5+ min later | Ended 3+ min earlier |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {'on time' if row.said_on_time else 'late'} | {row.lead_label} | {row.readings} "
            f"| {row.mean_abs_drift:.1f} | {row.p10:+.0f} | {row.median:+.0f} | {row.p90:+.0f} "
            f"| {row.share_5_later:.0%} | {row.share_earlier:.0%} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from sitt.config import load_settings

    parser = argparse.ArgumentParser(
        prog="python -m sitt.models.live_drift",
        description="How a live reading's delay changes before the train gets there.",
    )
    parser.add_argument("--db", type=Path, help="database (default SITT_DB_PATH)")
    parser.add_argument("--all", action="store_true", help="include long-distance trains")
    args = parser.parse_args(argv)
    db_path = args.db or load_settings().db_path
    if not Path(db_path).exists():
        print(f"No database at {db_path}.", file=sys.stderr)
        return 2
    with duckdb.connect(str(db_path), read_only=True) as con:
        rows = drift_table(con, suburban_only=not args.all)
    if not rows:
        print("No pairs of readings to compare yet. Collect a few rounds first.")
        return 1
    print(format_table(rows))
    print()
    print(
        f"Drift = final delay - the reading's delay. 'Final' is the last reading of the run "
        f"taken within {CLOSE_MINUTES} min of when the train was due."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
