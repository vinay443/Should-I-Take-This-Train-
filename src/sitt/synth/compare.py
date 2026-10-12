"""Does the synthetic data look like the real readings? A side-by-side summary.

    uv run python -m sitt.synth.compare
    uv run python -m sitt.synth.compare --db data/sitt.duckdb --synthetic-db data/synthetic.duckdb

For each direction and fast/slow, it compares how late trains are at the stations the real
data covers, in the hours of the day the real data covers (weekday or Sunday-timetable days
kept apart). Dates don't need to match: the synthetic weeks are filtered to the same kinds
of day and hours.

Real delay of a run: its last reading taken within `CLOSE_MINUTES` of the train's time (as
in sitt.models.live_drift). Synthetic delay of a run: its last reading at one of those
stations or a station next to them (the generator reports a train wherever it is at each
poll, so a run is rarely caught exactly at one station).

See docs/synthetic-vs-real.md for the first comparison and what it means.
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb

from sitt.holidays import is_sunday_schedule
from sitt.models.live_drift import CLOSE_MINUTES

GROUP_STATS = """
    count(*) AS runs, avg(d) AS mean, quantile_cont(d, 0.5) AS median,
    quantile_cont(d, 0.9) AS p90, avg((d <= 1)::INT) AS on_time, avg((d >= 5)::INT) AS late5,
    avg((d >= 10)::INT) AS late10
"""


@dataclass(frozen=True)
class Summary:
    direction: str
    train_type: str
    runs: int
    mean: float
    median: float
    p90: float
    on_time: float  # share at most 1 minute late
    late5: float
    late10: float


def real_runs(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """(train_id, direction, train_type, station_code, local timestamp, delay) per real run."""
    return con.execute(
        f"""
        WITH r AS (
            SELECT train_id, station_code, event, observed_at,
                   actual_or_expected_time AS t, delay_minutes AS d,
                   date_trunc('day', actual_or_expected_time
                       - to_minutes(CAST(delay_minutes AS BIGINT)) - INTERVAL 3 HOUR) AS run_key
            FROM observations
            WHERE delay_minutes IS NOT NULL AND actual_or_expected_time IS NOT NULL
              AND NOT coalesce(cancelled, false) AND event IN ('arrival', 'departure')
              AND source <> 'synthetic'
              AND train_id IN (SELECT train_id FROM trains)
        ), final AS (
            SELECT train_id, station_code, run_key,
                   arg_max(d, observed_at) AS d, arg_max(t, observed_at) AS t
            FROM r GROUP BY train_id, station_code, event, run_key
            HAVING max(observed_at) >= arg_max(t, observed_at) - INTERVAL {CLOSE_MINUTES} MINUTE
        ),
        -- arrival and departure of one run at one station count once
        runs AS (
            SELECT train_id, station_code, run_key, max(d) AS d, min(t) AS t
            FROM final GROUP BY ALL
        )
        SELECT train_id, tr.direction, tr.train_type, station_code,
               timezone('Asia/Kolkata', t) AS local_ts, d
        FROM runs JOIN trains tr USING (train_id)
        """
    ).fetchall()


def neighbours(con: duckdb.DuckDBPyConnection, codes: set[str]) -> set[str]:
    """The stations next to `codes` in line order, and the codes themselves."""
    rows = con.execute("SELECT code, seq FROM stations ORDER BY seq").fetchall()
    order = [code for code, _ in rows]
    found = set(codes)
    for i, code in enumerate(order):
        if code in codes:
            found.update(order[max(0, i - 1) : i + 2])
    return found


def synthetic_runs(con: duckdb.DuckDBPyConnection, stations: set[str]) -> list[tuple]:
    """(train_id, direction, train_type, station_code, local timestamp, delay) per run."""
    return con.execute(
        """
        WITH r AS (
            SELECT train_id, station_code, observed_at, delay_minutes AS d
            FROM observations
            WHERE station_code IN (SELECT unnest(?)) AND delay_minutes IS NOT NULL
              AND NOT coalesce(cancelled, false)
        ), runs AS (
            SELECT train_id,
                   CAST(timezone('Asia/Kolkata', observed_at) - INTERVAL 3 HOUR AS DATE) AS day,
                   arg_max(station_code, observed_at) AS station_code,
                   arg_max(d, observed_at) AS d, max(observed_at) AS ts
            FROM r GROUP BY ALL
        )
        SELECT train_id, tr.direction, tr.train_type, station_code,
               timezone('Asia/Kolkata', ts) AS local_ts, d
        FROM runs JOIN trains tr USING (train_id)
        """,
        [sorted(stations)],
    ).fetchall()


def _slot(local_ts) -> tuple[bool, int]:
    """(Sunday timetable, hour of day) of a naive Mumbai timestamp."""
    return is_sunday_schedule(local_ts.date()), local_ts.hour


def summarise(runs: list[tuple], slots: set[tuple[bool, int]] | None = None) -> list[Summary]:
    """Per direction and fast/slow, restricted to `slots` (kind of day, hour) if given."""
    kept = [
        (direction, kind, float(delay))
        for _, direction, kind, _, ts, delay in runs
        if slots is None or _slot(ts) in slots
    ]
    if not kept:
        return []
    with duckdb.connect() as mem:
        mem.execute("CREATE TABLE runs (direction VARCHAR, train_type VARCHAR, d DOUBLE)")
        mem.executemany("INSERT INTO runs VALUES (?, ?, ?)", kept)
        rows = mem.execute(
            f"SELECT direction, train_type, {GROUP_STATS} FROM runs GROUP BY ALL ORDER BY ALL"
        ).fetchall()
    return [Summary(*row) for row in rows]


def compare(
    real: duckdb.DuckDBPyConnection, synthetic: duckdb.DuckDBPyConnection
) -> tuple[list[Summary], list[Summary], set[str], set[tuple[bool, int]]]:
    """Real and synthetic summaries over the same stations, kinds of day and hours."""
    real_rows = real_runs(real)
    stations = {row[3] for row in real_rows}
    slots = {_slot(row[4]) for row in real_rows}
    synth_rows = synthetic_runs(synthetic, neighbours(synthetic, stations))
    return summarise(real_rows), summarise(synth_rows, slots), stations, slots


def format_comparison(real: list[Summary], synthetic: list[Summary]) -> str:
    keys = sorted({(s.direction, s.train_type) for s in real + synthetic})
    by_real = {(s.direction, s.train_type): s for s in real}
    by_synth = {(s.direction, s.train_type): s for s in synthetic}
    lines = [
        "| Direction | Type | Data | Runs | Mean | Median | 90th pct | On time (≤1) "
        "| 5+ late | 10+ late |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key in keys:
        for label, table in (("real", by_real), ("synthetic", by_synth)):
            s = table.get(key)
            if s is None:
                lines.append(f"| {key[0]} | {key[1]} | {label} | 0 | | | | | | |")
                continue
            lines.append(
                f"| {s.direction} | {s.train_type} | {label} | {s.runs} | {s.mean:.1f} "
                f"| {s.median:.0f} | {s.p90:.0f} | {s.on_time:.0%} | {s.late5:.0%} "
                f"| {s.late10:.0%} |"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from sitt.config import load_settings

    parser = argparse.ArgumentParser(
        prog="python -m sitt.synth.compare",
        description="Compare synthetic delays with the real readings.",
    )
    parser.add_argument("--db", type=Path, help="real database (default SITT_DB_PATH)")
    parser.add_argument(
        "--synthetic-db", type=Path, default=Path("data/synthetic.duckdb"), help="synthetic DB"
    )
    args = parser.parse_args(argv)
    real_path = args.db or load_settings().db_path
    for path in (real_path, args.synthetic_db):
        if not Path(path).exists():
            print(f"No database at {path}.", file=sys.stderr)
            return 2
    with (
        duckdb.connect(str(real_path), read_only=True) as real,
        duckdb.connect(str(args.synthetic_db), read_only=True) as synthetic,
    ):
        real_summary, synth_summary, stations, slots = compare(real, synthetic)
    if not real_summary:
        print("No real runs to compare yet. Collect some readings first.")
        return 1
    hours = sorted({hour for _, hour in slots})
    days = sorted({"Sunday timetable" if sunday else "weekday" for sunday, _ in slots})
    print(
        f"Stations: {', '.join(sorted(stations))}. Hours: {', '.join(f'{h:02d}' for h in hours)} "
        f"({', '.join(days)})."
    )
    print()
    print(format_comparison(real_summary, synth_summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
