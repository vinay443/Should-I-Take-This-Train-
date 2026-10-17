"""How late trains have really been: delay by hour and direction, from real readings.

Each *run* (one train at one station on one service day) counts once, with the delay of its
last reading, and only if that reading was taken close to the train's time: the same rule
as docs/live-drift.md (sitt.models.live_drift.runs_ctes). Counting readings instead would
weight a train by how many times the collector happened to see it, and early "on time"
readings understate delays.

Only real (not synthetic) readings of suburban trains are used. Small cells are kept but
marked, so the page can say how little a number rests on.

Weekdays (Monday to Friday) and weekends (Saturday, Sunday and the holidays in
sitt.holidays) are also given apart, by the run's service day, but only once there are
readings on at least SPLIT_MIN_DAYS days of each: before that a split would show noise.
"""

import math
from dataclasses import asdict, dataclass

import duckdb

from sitt.holidays import FIXED_HOLIDAYS, movable_dates
from sitt.models.live_drift import CLOSE_MINUTES, runs_ctes
from sitt.tz import mumbai_sql

# A cell with fewer runs than this is shown, but marked as too few to read much into.
MIN_RUNS = 5
LATE_MINUTES = 5  # "late" for the share of late runs

DIRECTIONS = {"up": "Towards CSMT", "down": "Away from CSMT"}

# Weekdays vs weekends and holidays: shown apart only with readings on this many days of each.
SPLIT_MIN_DAYS = 2
DAY_KINDS = {"weekday": "Weekdays", "weekend": "Weekends & holidays"}

# Parts of the day, by scheduled hour at the station. Hours run 0-23; 'night' wraps.
BANDS = (
    ("early", "Early morning", 4, 7),
    ("am_peak", "Morning peak", 7, 11),
    ("midday", "Midday", 11, 16),
    ("pm_peak", "Evening peak", 16, 21),
    ("night", "Night", 21, 4),
)


@dataclass(frozen=True)
class Cell:
    direction: str
    key: str  # an hour ("7") or a band key ("am_peak")
    runs: int
    median: float
    p90: float
    share_late: float  # share of runs at least LATE_MINUTES late
    few: bool  # fewer than MIN_RUNS runs


def _band_sql() -> str:
    cases = []
    for key, _label, start, end in BANDS:
        if start < end:
            cases.append(f"WHEN hour >= {start} AND hour < {end} THEN '{key}'")
        else:
            cases.append(f"WHEN hour >= {start} OR hour < {end} THEN '{key}'")
    return "CASE " + " ".join(cases) + " END"


def _kind_sql(day: str) -> str:
    """'weekend' for a Saturday, Sunday or holiday (fixed or movable), else 'weekday'."""
    fixed = ", ".join(f"'{month:02d}-{dom:02d}'" for month, dom in sorted(FIXED_HOLIDAYS))
    movable = ", ".join(f"DATE '{d.isoformat()}'" for d in sorted(movable_dates()))
    holiday = f"strftime({day}, '%m-%d') IN ({fixed})"
    if movable:
        holiday += f" OR {day} IN ({movable})"
    return f"CASE WHEN isodow({day}) >= 6 OR {holiday} THEN 'weekend' ELSE 'weekday' END"


def _runs_sql(kind: str | None = None) -> str:
    """One row per run: direction, train type, station, scheduled local hour, final delay,
    service day and its kind. `kind` keeps only weekday or weekend runs."""
    local = mumbai_sql("f.scheduled")
    where = f"WHERE kind = '{kind}'" if kind else ""
    if kind not in (None, *DAY_KINDS):
        raise ValueError(f"unknown kind of day: {kind}")
    return f"""
        WITH {runs_ctes(suburban_only=True)}, days AS (
            SELECT t.direction, t.train_type, f.station_code, f.d_final AS delay,
                   hour({local}) AS hour,
                   -- The service day (03:00 to 03:00), so a 00:30 train is part of the
                   -- day before, as its run is.
                   CAST(f.run_key AS DATE) AS day, f.last_seen
            FROM final f JOIN trains t USING (train_id)
        ), runs AS (
            SELECT * FROM (SELECT *, {_kind_sql("day")} AS kind FROM days) {where}
        )
    """


def _cells(con: duckdb.DuckDBPyConnection, group: str, kind: str | None = None) -> list[Cell]:
    rows = con.execute(
        f"""
        {_runs_sql(kind)}
        SELECT direction, CAST({group} AS VARCHAR) AS key, count(*), median(delay),
               quantile_cont(delay, 0.9), avg((delay >= {LATE_MINUTES})::INT)
        FROM runs GROUP BY ALL ORDER BY direction, min(hour)
        """
    ).fetchall()
    return [
        Cell(direction, key, int(n), float(med), float(p90), float(late), int(n) < MIN_RUNS)
        for direction, key, n, med, p90, late in rows
    ]


def _section(con: duckdb.DuckDBPyConnection, kind: str | None = None) -> dict:
    """The numbers the page draws, for all runs or for one kind of day."""
    hours = _cells(con, "hour", kind)
    bands = _cells(con, _band_sql(), kind)
    runs, days, stations, first, last = con.execute(
        f"""
        {_runs_sql(kind)}
        SELECT count(*), count(DISTINCT day),
               list(DISTINCT station_code ORDER BY station_code), min(day), max(day)
        FROM runs
        """
    ).fetchone()
    by_type = con.execute(
        f"""
        {_runs_sql(kind)}
        SELECT train_type, count(*), median(delay), avg((delay >= {LATE_MINUTES})::INT)
        FROM runs GROUP BY ALL ORDER BY train_type
        """
    ).fetchall()
    by_direction = con.execute(
        f"""
        {_runs_sql(kind)}
        SELECT direction, count(*), median(delay), quantile_cont(delay, 0.9),
               avg((delay >= {LATE_MINUTES})::INT)
        FROM runs GROUP BY ALL ORDER BY direction DESC
        """
    ).fetchall()
    return {
        "runs": int(runs),
        "days": int(days),
        "stations": stations or [],
        "first_day": first.isoformat() if first else None,
        "last_day": last.isoformat() if last else None,
        "by_band": [asdict(cell) for cell in bands],
        "by_hour": [asdict(cell) | {"key": int(cell.key)} for cell in hours],
        "by_direction": [
            {
                "direction": direction,
                "runs": int(n),
                "median": float(med),
                "p90": float(p90),
                "share_late": float(late),
                "few": int(n) < MIN_RUNS,
            }
            for direction, n, med, p90, late in by_direction
        ],
        "by_type": [
            {"type": kind_, "runs": int(n), "median": float(med), "share_late": float(late)}
            for kind_, n, med, late in by_type
        ],
    }


def delay_patterns(con: duckdb.DuckDBPyConnection) -> dict:
    """Everything the /patterns page shows, as JSON-ready data.

    The top-level numbers cover every day. `day_kinds` says how many days and runs of each
    kind there are; `by_kind` has the same numbers per kind, only once `split_ready`.
    """
    counts = dict(
        (kind, (int(days), int(runs)))
        for kind, days, runs in con.execute(
            f"{_runs_sql()} SELECT kind, count(DISTINCT day), count(*) FROM runs GROUP BY kind"
        ).fetchall()
    )
    day_kinds = [
        {
            "key": key,
            "label": label,
            "days": counts.get(key, (0, 0))[0],
            "runs": counts.get(key, (0, 0))[1],
        }
        for key, label in DAY_KINDS.items()
    ]
    split_ready = all(k["days"] >= SPLIT_MIN_DAYS for k in day_kinds)
    return {
        **_section(con),
        "min_runs": MIN_RUNS,
        "late_minutes": LATE_MINUTES,
        "close_minutes": CLOSE_MINUTES,
        "directions": [{"key": key, "label": label} for key, label in DIRECTIONS.items()],
        "bands": [
            {"key": key, "label": label, "from": start, "to": end}
            for key, label, start, end in BANDS
        ],
        "day_kinds": day_kinds,
        "split_min_days": SPLIT_MIN_DAYS,
        "split_ready": split_ready,
        "by_kind": {key: _section(con, key) for key in DAY_KINDS} if split_ready else {},
    }


def _kinds_text(data: dict) -> str:
    """'3 weekdays and 1 weekend day or holiday'."""
    counts = {k["key"]: k["days"] for k in data["day_kinds"]}
    weekdays, weekend = counts["weekday"], counts["weekend"]
    return (
        f"{weekdays} weekday{'' if weekdays == 1 else 's'} and {weekend} weekend "
        f"day{'' if weekend == 1 else 's'} or holiday{'' if weekend == 1 else 's'}"
    )


def split_note(data: dict) -> str:
    """One sentence on whether weekdays and weekends can be shown apart yet."""
    if data["split_ready"]:
        return (
            f"Readings on {_kinds_text(data)}: use --days weekday or --days weekend "
            "to see them apart."
        )
    return (
        f"Weekdays and weekends are shown apart once there are readings on at least "
        f"{data['split_min_days']} of each; so far {_kinds_text(data)}."
    )


def format_report(data: dict, markdown: bool = False, days: str | None = None) -> str:
    """The /patterns page as text: a summary line, the part-of-day table, fast vs slow.

    `days` ('weekday' or 'weekend') shows only that kind of day; it needs `split_ready`.
    """
    if not data["runs"]:
        return "No real readings yet: run `uv run sitt-collect` a few times, then try again."
    if days:
        if not data["split_ready"]:
            return split_note(data)
        section = data["by_kind"][days]
        label = DAY_KINDS[days]
        data = data | section
        if not data["runs"]:  # can't happen while split_ready, but say so plainly
            return f"No trains seen on {label.lower()}."
    labels = {d["key"]: d["label"] for d in data["directions"]}
    cells = {(c["direction"], c["key"]): c for c in data["by_band"]}
    span = data["first_day"] + (
        f" to {data['last_day']}" if data["last_day"] != data["first_day"] else ""
    )
    lines = [
        f"{data['runs']} trains seen at {', '.join(data['stations'])}, {span} "
        f"({data['days']} day{'s' if data['days'] != 1 else ''}). Median delay; share "
        f"{data['late_minutes']}+ min late; trains. * = fewer than {data['min_runs']} trains.",
        "",
    ]
    if days:
        lines[0] = f"{label} only: {lines[0]}"

    def cell_text(cell: dict | None) -> str:
        if cell is None:
            return "-"
        star = "*" if cell["few"] else ""
        return f"{_minutes(cell['median'])}, {_percent(cell['share_late'])}, {cell['runs']}{star}"

    header = ["Scheduled", *labels.values()]
    rows = [
        [f"{band['label']} ({band['from']:02d}-{band['to']:02d})"]
        + [cell_text(cells.get((key, band["key"]))) for key in labels]
        for band in data["bands"]
    ]
    lines += _table(header, rows, markdown)
    lines.append("")
    lines += _table(
        ["Trains", "Median", f"{data['late_minutes']}+ min late", "Trains seen"],
        [
            [t["type"], _minutes(t["median"]), _percent(t["share_late"]), str(t["runs"])]
            for t in data["by_type"]
        ],
        markdown,
    )
    if not days:
        lines += ["", split_note(data)]
    return "\n".join(lines)


def _minutes(value: float) -> str:
    """Rounded half up, like the page (Python's round() would make 2.5 into 2)."""
    return f"{math.floor(value + 0.5)} min"


def _percent(share: float) -> str:
    """Rounded half up, like the page: 1 in 8 is 13%, not 12%."""
    return f"{math.floor(share * 100 + 0.5)}%"


def _table(header: list[str], rows: list[list[str]], markdown: bool) -> list[str]:
    if markdown:
        return [
            "| " + " | ".join(header) + " |",
            "|" + "|".join([" --- "] + [" ---: "] * (len(header) - 1)) + "|",
            *("| " + " | ".join(row) + " |" for row in rows),
        ]
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    return [fmt.format(*header).rstrip(), *(fmt.format(*row).rstrip() for row in rows)]


def main(argv: list[str] | None = None) -> int:
    """`sitt-patterns`: how late trains have been, from real readings (as /patterns shows)."""
    import argparse
    from pathlib import Path

    from sitt.config import load_settings
    from sitt.db import open_with_retry

    parser = argparse.ArgumentParser(
        prog="sitt-patterns",
        description="How late trains have really been, by part of the day and direction.",
    )
    parser.add_argument("--db", type=Path, help="database (default SITT_DB_PATH)")
    parser.add_argument("--markdown", action="store_true", help="Markdown tables, for docs")
    parser.add_argument(
        "--days",
        choices=("all", *DAY_KINDS),
        default="all",
        help="only weekdays (Mon-Fri) or weekends and holidays; needs readings on "
        f"{SPLIT_MIN_DAYS}+ days of each",
    )
    args = parser.parse_args(argv)
    db = args.db or load_settings().db_path
    if not Path(db).exists():
        print(f"No database at {db}. Run `uv run sitt-init-db` first.")
        return 2
    con = open_with_retry(Path(db), read_only=True)
    try:
        data = delay_patterns(con)
    finally:
        con.close()
    days = None if args.days == "all" else args.days
    print(format_report(data, args.markdown, days))
    if days and not data["split_ready"]:
        return 1
    return 0 if data["runs"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
