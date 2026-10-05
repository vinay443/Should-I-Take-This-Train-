"""Data-quality checks on `observations`.

    uv run sitt-dq                       report on every real observation
    uv run sitt-dq --hours 24            ... on the last 24 hours
    uv run sitt-dq --markdown            also write scratch/dq-report.md
    uv run sitt-dq --write-flags         store the flags in `dq_flags`

Nothing is ever deleted or changed in `observations`. A suspect reading is *flagged*:
`--write-flags` records (observation id, flag, detail) in the `dq_flags` table, and the
feature builder (sitt.models.features.prepare) leaves flagged readings out by default.

Row flags (each explained in docs/data-quality.md):

    exact_duplicate      the same reading stored twice
    near_duplicate       the same train, station and event read again within minutes by
                         another batch (two collectors running at once)
    implausible_delay    a delay outside the configured bounds
    delay_jump           the delay changed by more than the time that passed allows
    schedule_mismatch    the source's delay disagrees with our timetable's
    far_from_schedule    read hours away from when the train was due: wrong-day match?
    future_timestamp     stamped later than now
    batch_time_mismatch  stamped far from its batch's own time (a time-zone mistake?)

Reported but not flagged per row: train numbers and station codes that aren't in the
timetable, cancelled / less-accurate rates per source, and small differences between a
source's schedule and ours.

Synthetic observations are left out unless asked for. Bounds are `DQSettings` in
sitt.config. All times shown are Asia/Kolkata.
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
from dotenv import find_dotenv, load_dotenv

from sitt.config import DQSettings, load_dq_settings, load_settings
from sitt.db import DatabaseBusyError, open_with_retry
from sitt.tz import IST

IST_OFFSET_MS = 19_800_000
DEFAULT_MARKDOWN = Path("scratch") / "dq-report.md"
EXAMPLES = 5

FLAGS = (
    "exact_duplicate",
    "near_duplicate",
    "implausible_delay",
    "delay_jump",
    "schedule_mismatch",
    "far_from_schedule",
    "future_timestamp",
    "batch_time_mismatch",
)

# A Central Railway suburban train number: 95xxx-99xxx. One of these missing from the
# timetable means the timetable is out of date; other numbers are long-distance trains.
SUBURBAN_NUMBER = r"^9[5-9]\d{3}$"


def _has_table(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return (
        con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]
        ).fetchone()[0]
        > 0
    )


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _local_minute(column: str) -> str:
    """SQL: minute of the Mumbai day for an epoch-milliseconds column."""
    return f"(CAST(floor(({column} + {IST_OFFSET_MS}) / 60000.0) AS BIGINT) % 1440)"


def _wrap(expression: str) -> str:
    """SQL: a difference in minutes of the day, folded into -720..719."""
    return f"(((({expression}) + 720) % 1440 + 1440) % 1440 - 720)"


_PARTITION = "PARTITION BY source, train_number, station_code, event ORDER BY obs_ms, id"


def find_flags(
    con: duckdb.DuckDBPyConnection,
    settings: DQSettings | None = None,
    now: datetime | None = None,
    include_synthetic: bool = False,
) -> int:
    """Run every row check over all observations. Leaves the result in two temp tables:

    `dq_obs` (the observations checked) and `dq_found` (id, flag, detail). Returns the
    number of flags found. Works on a read-only connection.
    """
    s = settings or DQSettings()
    now = now or datetime.now(UTC)
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE dq_obs AS
        SELECT id, source, batch_id, train_id, train_number, station_code, event,
               delay_minutes, coalesce(cancelled, false) AS cancelled,
               coalesce(less_accurate, false) AS less_accurate,
               epoch_ms(observed_at) AS obs_ms,
               epoch_ms(actual_or_expected_time) AS time_ms,
               epoch_ms(try_strptime(substr(batch_id, 1, 15), '%Y%m%dT%H%M%S')) AS batch_ms,
               train_number IS NOT NULL AND train_id <> train_number AS matched
        FROM observations
        WHERE ? OR source <> 'synthetic'
        """,
        [include_synthetic],
    )
    has_timetable = _has_table(con, "scheduled_stops")
    schedule = (
        """
        SELECT o.*,
               CASE WHEN o.event = 'departure' THEN hour(st.depart) * 60 + minute(st.depart)
                    ELSE hour(st.arrive) * 60 + minute(st.arrive) END AS sched_min
        FROM dq_obs o
        JOIN (SELECT train_id, station_code,
                     coalesce(scheduled_arrival, scheduled_departure) AS arrive,
                     coalesce(scheduled_departure, scheduled_arrival) AS depart
              FROM scheduled_stops) st
          ON st.train_id = o.train_id AND st.station_code = o.station_code
        WHERE o.matched AND NOT o.cancelled
        """
        if has_timetable
        else "SELECT *, NULL::BIGINT AS sched_min FROM dq_obs WHERE false"
    )
    con.execute(f"CREATE OR REPLACE TEMP TABLE dq_sched AS {schedule}")
    implied = _wrap(f"{_local_minute('time_ms')} - sched_min")
    away = _wrap(
        f"{_local_minute('obs_ms')} - sched_min - CAST(round(coalesce(delay_minutes, 0)) AS BIGINT)"
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE dq_found AS
        SELECT id, 'exact_duplicate' AS flag,
               'the same reading as observation ' || first_id AS detail
        FROM (SELECT id, first_value(id) OVER (
                         PARTITION BY source, train_number, station_code, event, obs_ms,
                                      time_ms, delay_minutes, cancelled ORDER BY id) AS first_id
              FROM dq_obs)
        WHERE id <> first_id

        UNION ALL
        SELECT id, 'near_duplicate',
               'read ' || round((obs_ms - prev_ms) / 60000.0, 1) || ' min after batch '
                   || coalesce(prev_batch, '?')
        FROM (SELECT id, obs_ms, batch_id, lag(obs_ms) OVER w AS prev_ms,
                     lag(batch_id) OVER w AS prev_batch
              FROM dq_obs WINDOW w AS ({_PARTITION}))
        WHERE prev_ms IS NOT NULL AND batch_id IS DISTINCT FROM prev_batch
          AND obs_ms - prev_ms <= $near_ms

        UNION ALL
        SELECT id, 'implausible_delay',
               'delay ' || delay_minutes || ' min; allowed '
                   || CASE WHEN matched THEN $early ELSE $early_ld END || ' early to '
                   || CASE WHEN matched THEN $late ELSE $late_ld END || ' late'
        FROM dq_obs
        WHERE delay_minutes IS NOT NULL
          AND (delay_minutes < -CASE WHEN matched THEN $early ELSE $early_ld END
               OR delay_minutes > CASE WHEN matched THEN $late ELSE $late_ld END)

        UNION ALL
        SELECT id, 'delay_jump',
               'delay went from ' || prev_delay || ' to ' || delay_minutes || ' min in '
                   || round((obs_ms - prev_ms) / 60000.0, 1) || ' min'
        FROM (SELECT id, obs_ms, delay_minutes, lag(obs_ms) OVER w AS prev_ms,
                     lag(delay_minutes) OVER w AS prev_delay
              FROM dq_obs WHERE delay_minutes IS NOT NULL AND NOT cancelled
              WINDOW w AS ({_PARTITION}))
        WHERE prev_ms IS NOT NULL AND obs_ms - prev_ms <= $jump_window_ms
          AND abs(delay_minutes - prev_delay) > (obs_ms - prev_ms) / 60000.0 + $jump_slack

        UNION ALL
        SELECT id, 'schedule_mismatch',
               'source says ' || delay_minutes || ' min late; our timetable implies '
                   || {implied}
        FROM dq_sched
        WHERE time_ms IS NOT NULL AND delay_minutes IS NOT NULL
          AND abs({implied} - delay_minutes) > $mismatch

        UNION ALL
        SELECT id, 'far_from_schedule',
               'read ' || {away} || ' min from when the train was due'
        FROM dq_sched
        WHERE abs({away}) > $far

        UNION ALL
        SELECT id, 'future_timestamp',
               'stamped ' || round((obs_ms - $now_ms) / 60000.0, 1) || ' min in the future'
        FROM dq_obs WHERE obs_ms > $now_ms + $future_ms

        UNION ALL
        SELECT id, 'batch_time_mismatch',
               'stamped ' || round((obs_ms - batch_ms) / 60000.0) || ' min from its batch'
        FROM dq_obs
        WHERE batch_ms IS NOT NULL AND abs(obs_ms - batch_ms) > $batch_ms_tolerance
        """,
        {
            "near_ms": s.near_duplicate_minutes * 60_000,
            "early": s.max_early_minutes,
            "late": s.max_delay_minutes,
            "early_ld": s.max_early_minutes_long_distance,
            "late_ld": s.max_delay_minutes_long_distance,
            "jump_window_ms": s.jump_window_minutes * 60_000,
            "jump_slack": s.jump_slack_minutes,
            "mismatch": s.schedule_mismatch_minutes,
            "far": s.far_from_schedule_minutes,
            "now_ms": _ms(now),
            "future_ms": s.future_tolerance_minutes * 60_000,
            "batch_ms_tolerance": s.batch_time_tolerance_minutes * 60_000,
        },
    )
    return con.execute("SELECT count(*) FROM dq_found").fetchone()[0]


def write_flags(con: duckdb.DuckDBPyConnection, now: datetime | None = None) -> int:
    """Replace `dq_flags` with what `find_flags` just found. Returns the rows written.

    `dq_flags` is derived from `observations`, so replacing it loses nothing; the
    observations themselves are never touched.
    """
    now = now or datetime.now(UTC)
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM dq_flags")
        con.execute(
            "INSERT INTO dq_flags (observation_id, flag, detail, flagged_at) "
            "SELECT id, flag, any_value(detail), ? FROM dq_found GROUP BY id, flag",
            [now],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return con.execute("SELECT count(*) FROM dq_flags").fetchone()[0]


def refresh_flags(
    con: duckdb.DuckDBPyConnection,
    settings: DQSettings | None = None,
    now: datetime | None = None,
    include_synthetic: bool = False,
) -> int:
    """Recompute and store every flag. For callers that are about to build features."""
    find_flags(con, settings, now, include_synthetic)
    return write_flags(con, now)


# --- the report ---


@dataclass(frozen=True)
class SourceRates:
    source: str
    rows: int
    cancelled: int
    less_accurate: int
    without_delay: int


@dataclass
class DQReport:
    start: datetime | None
    end: datetime
    rows: int
    flagged_rows: int
    flag_counts: dict[str, int]
    examples: dict[str, list[str]]
    sources: list[SourceRates]
    # (train number, readings) for numbers not in the timetable.
    unmatched_suburban: list[tuple[str, int]]
    unmatched_other: list[tuple[str, int]]
    unmatched_stations: list[tuple[str, int]]
    # (train number, station, minutes our timetable differs from the source's schedule)
    schedule_drift: list[tuple[str, str, float]]
    settings: DQSettings = field(default_factory=DQSettings)

    @property
    def flagged_share(self) -> float:
        return self.flagged_rows / self.rows if self.rows else 0.0


def build_report(
    con: duckdb.DuckDBPyConnection,
    start: datetime | None = None,
    end: datetime | None = None,
    settings: DQSettings | None = None,
    now: datetime | None = None,
    include_synthetic: bool = False,
) -> DQReport:
    """Check everything, then summarise the readings observed in [start, end]."""
    settings = settings or DQSettings()
    now = now or datetime.now(UTC)
    end = end or now
    find_flags(con, settings, now, include_synthetic)
    window = {"start": _ms(start) if start else None, "end": _ms(end)}
    in_window = "($start IS NULL OR obs_ms >= $start) AND obs_ms <= $end"
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE dq_window AS SELECT * FROM dq_obs WHERE {in_window}",
        window,
    )

    rows = con.execute("SELECT count(*) FROM dq_window").fetchone()[0]
    flag_rows = con.execute(
        "SELECT f.flag, count(*) FROM dq_found f JOIN dq_window w USING (id) GROUP BY f.flag"
    ).fetchall()
    flagged = con.execute(
        "SELECT count(DISTINCT f.id) FROM dq_found f JOIN dq_window w USING (id)"
    ).fetchone()[0]
    examples: dict[str, list[str]] = {}
    for flag, _count in flag_rows:
        found = con.execute(
            "SELECT w.train_number, w.station_code, w.event, w.source, f.detail, w.obs_ms "
            "FROM dq_found f JOIN dq_window w USING (id) WHERE f.flag = ? "
            "ORDER BY w.obs_ms DESC, w.id LIMIT ?",
            [flag, EXAMPLES],
        ).fetchall()
        examples[flag] = [
            f"{number} {event or ''} at {station or '-'} ({source}, "
            f"{datetime.fromtimestamp(obs_ms / 1000, IST):%d %b %H:%M}): {detail}"
            for number, station, event, source, detail, obs_ms in found
        ]

    sources = [
        SourceRates(*row)
        for row in con.execute(
            "SELECT source, count(*), count(*) FILTER (WHERE cancelled), "
            "count(*) FILTER (WHERE less_accurate), "
            "count(*) FILTER (WHERE delay_minutes IS NULL AND NOT cancelled) "
            "FROM dq_window GROUP BY source ORDER BY source"
        ).fetchall()
    ]
    unmatched = (
        "SELECT train_number, count(*) FROM dq_window "
        "WHERE NOT matched AND train_number IS NOT NULL AND {} regexp_matches(train_number, ?) "
        "GROUP BY train_number ORDER BY count(*) DESC, train_number"
    )
    unmatched_suburban = con.execute(unmatched.format(""), [SUBURBAN_NUMBER]).fetchall()
    unmatched_other = con.execute(unmatched.format("NOT"), [SUBURBAN_NUMBER]).fetchall()
    stations_known = (
        "station_code NOT IN (SELECT code FROM stations)" if _has_table(con, "stations") else "true"
    )
    unmatched_stations = con.execute(
        f"SELECT station_code, count(*) FROM dq_window WHERE station_code <> '' "
        f"AND {stations_known} GROUP BY station_code ORDER BY count(*) DESC, station_code"
    ).fetchall()

    implied = _wrap(f"{_local_minute('time_ms')} - sched_min")
    schedule_drift = con.execute(
        f"""
        SELECT train_number, station_code, median({implied} - delay_minutes) AS drift
        FROM dq_sched
        WHERE time_ms IS NOT NULL AND delay_minutes IS NOT NULL
          AND id IN (SELECT id FROM dq_window)
        GROUP BY train_number, station_code
        HAVING abs(median({implied} - delay_minutes)) BETWEEN 2 AND ?
        ORDER BY abs(drift) DESC, train_number
        """,
        [settings.schedule_mismatch_minutes],
    ).fetchall()

    return DQReport(
        start=start,
        end=end,
        rows=rows,
        flagged_rows=flagged,
        flag_counts={flag: count for flag, count in sorted(flag_rows)},
        examples=examples,
        sources=sources,
        unmatched_suburban=unmatched_suburban,
        unmatched_other=unmatched_other,
        unmatched_stations=unmatched_stations,
        schedule_drift=schedule_drift,
        settings=settings,
    )


def _percent(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


def _listed(items: Sequence[tuple], limit: int = 8) -> str:
    shown = ", ".join(f"{name or '(blank)'} ({count})" for name, count in items[:limit])
    return shown + (f", and {len(items) - limit} more" if len(items) > limit else "")


def headline_lines(report: DQReport) -> list[str]:
    """Two or three lines for the health summary."""
    if report.rows == 0:
        return ["no readings to check"]
    flags = ", ".join(f"{flag} {count}" for flag, count in report.flag_counts.items())
    lines = [
        f"{report.flagged_rows:,} of {report.rows:,} readings flagged "
        f"({_percent(report.flagged_rows, report.rows)})" + (f": {flags}" if flags else "")
    ]
    lines.append(
        f"not in the timetable: {len(report.unmatched_suburban)} suburban train number(s), "
        f"{len(report.unmatched_other)} long-distance, "
        f"{len(report.unmatched_stations)} station code(s)"
    )
    return lines


def headline(con: duckdb.DuckDBPyConnection, start: datetime, end: datetime) -> list[str]:
    """The hook `sitt-health` calls (see sitt.health.build_report)."""
    return headline_lines(build_report(con, start, end, load_dq_settings(), now=end))


def _window_text(report: DQReport) -> str:
    end = f"{report.end.astimezone(IST):%d %b %Y %H:%M}"
    if report.start is None:
        return f"all readings up to {end} IST"
    return f"{report.start.astimezone(IST):%d %b %Y %H:%M} to {end} IST"


def format_report(report: DQReport, markdown: bool = False) -> str:
    """The report as plain text, or as Markdown."""
    h1, h2, item, sub = ("# ", "## ", "- ", "  - ") if markdown else ("", "", "  ", "      ")
    gap = [""] if markdown else []
    lines = [f"{h1}Observation data quality: {_window_text(report)}", *gap]
    lines.append(
        f"{report.rows:,} real readings checked; {report.flagged_rows:,} flagged "
        f"({_percent(report.flagged_rows, report.rows)}). Synthetic readings are not included."
    )
    lines += ["", f"{h2}Flags", *gap]
    if not report.flag_counts:
        lines.append(f"{item}none")
    for flag in FLAGS:
        if flag not in report.flag_counts:
            continue
        lines.append(f"{item}{flag}: {report.flag_counts[flag]:,}")
        lines += [f"{sub}{example}" for example in report.examples.get(flag, [])]

    lines += ["", f"{h2}Not in the timetable", *gap]
    lines.append(
        f"{item}Suburban train numbers (95xxx-99xxx): {len(report.unmatched_suburban)}"
        + (f": {_listed(report.unmatched_suburban)}" if report.unmatched_suburban else "")
    )
    lines.append(
        f"{item}Other train numbers (long-distance, expected): {len(report.unmatched_other)}"
        + (f": {_listed(report.unmatched_other)}" if report.unmatched_other else "")
    )
    lines.append(
        f"{item}Station codes: {len(report.unmatched_stations)}"
        + (f": {_listed(report.unmatched_stations)}" if report.unmatched_stations else "")
    )

    lines += ["", f"{h2}Sources", *gap]
    for rates in report.sources:
        lines.append(
            f"{item}{rates.source}: {rates.rows:,} readings; cancelled "
            f"{_percent(rates.cancelled, rates.rows)}; less accurate "
            f"{_percent(rates.less_accurate, rates.rows)}; no delay given "
            f"{_percent(rates.without_delay, rates.rows)}"
        )
    if not report.sources:
        lines.append(f"{item}no readings")

    lines += ["", f"{h2}Schedule differences (not flagged)", *gap]
    if report.schedule_drift:
        lines.append(
            f"{item}{len(report.schedule_drift)} train(s) whose source schedule differs from "
            f"our timetable by 2 to {report.settings.schedule_mismatch_minutes:g} min. The "
            "timetable may be out of date for them:"
        )
        lines += [
            f"{sub}{number} at {station}: {drift:+g} min"
            for number, station, drift in report.schedule_drift[:EXAMPLES]
        ]
    else:
        lines.append(f"{item}none of 2 minutes or more")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sitt-dq",
        description="Check the collected observations for duplicates, implausible delays, "
        "unmatched trains and stations, and timestamp mistakes. Nothing is deleted.",
        epilog="Bounds are set with SITT_DQ_* variables. See docs/data-quality.md.",
    )
    parser.add_argument("--db", type=Path, help="default: SITT_DB_PATH or data/sitt.duckdb")
    parser.add_argument(
        "--hours", type=float, help="report only on the last this-many hours (default: all)"
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        nargs="?",
        const=DEFAULT_MARKDOWN,
        help=f"also write the report as Markdown (default path: {DEFAULT_MARKDOWN.as_posix()})",
    )
    parser.add_argument(
        "--write-flags",
        action="store_true",
        help="store the flags in dq_flags, replacing earlier ones. Always covers all "
        "observations, whatever --hours says",
    )
    args = parser.parse_args(argv)

    load_dotenv(find_dotenv(usecwd=True))
    try:
        settings = load_dq_settings()
        db_path = args.db or load_settings().db_path
    except ValueError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    now = now or datetime.now(UTC)
    start = now - timedelta(hours=args.hours) if args.hours else None

    try:
        with open_with_retry(db_path, read_only=not args.write_flags) as con:
            report = build_report(con, start, now, settings, now)
            written = write_flags(con, now) if args.write_flags else None
    except FileNotFoundError as exc:
        print(f"{exc}. Run `uv run sitt-init-db` or `uv run sitt-collect` first.", file=sys.stderr)
        return 2
    except DatabaseBusyError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(format_report(report))
    if written is not None:
        print(f"\nStored {written:,} flag(s) in dq_flags.")
    if args.markdown is not None:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(format_report(report, markdown=True) + "\n", encoding="utf-8")
        print(f"\nWrote {args.markdown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
