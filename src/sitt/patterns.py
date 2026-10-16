"""How late trains have really been: delay by hour and direction, from real readings.

Each *run* (one train at one station on one service day) counts once, with the delay of its
last reading, and only if that reading was taken close to the train's time: the same rule
as docs/live-drift.md (sitt.models.live_drift.runs_ctes). Counting readings instead would
weight a train by how many times the collector happened to see it, and early "on time"
readings understate delays.

Only real (not synthetic) readings of suburban trains are used. Small cells are kept but
marked, so the page can say how little a number rests on.
"""

import math
from dataclasses import asdict, dataclass

import duckdb

from sitt.models.live_drift import CLOSE_MINUTES, runs_ctes
from sitt.tz import mumbai_sql

# A cell with fewer runs than this is shown, but marked as too few to read much into.
MIN_RUNS = 5
LATE_MINUTES = 5  # "late" for the share of late runs

DIRECTIONS = {"up": "Towards CSMT", "down": "Away from CSMT"}

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


def _runs_sql() -> str:
    """One row per run: direction, train type, station, scheduled local hour, final delay."""
    local = mumbai_sql("f.scheduled")
    return f"""
        WITH {runs_ctes(suburban_only=True)}, runs AS (
            SELECT t.direction, t.train_type, f.station_code, f.d_final AS delay,
                   hour({local}) AS hour, CAST({local} AS DATE) AS day, f.last_seen
            FROM final f JOIN trains t USING (train_id)
        )
    """


def _cells(con: duckdb.DuckDBPyConnection, group: str) -> list[Cell]:
    rows = con.execute(
        f"""
        {_runs_sql()}
        SELECT direction, CAST({group} AS VARCHAR) AS key, count(*), median(delay),
               quantile_cont(delay, 0.9), avg((delay >= {LATE_MINUTES})::INT)
        FROM runs GROUP BY ALL ORDER BY direction, min(hour)
        """
    ).fetchall()
    return [
        Cell(direction, key, int(n), float(med), float(p90), float(late), int(n) < MIN_RUNS)
        for direction, key, n, med, p90, late in rows
    ]


def delay_patterns(con: duckdb.DuckDBPyConnection) -> dict:
    """Everything the /patterns page shows, as JSON-ready data."""
    hours = _cells(con, "hour")
    bands = _cells(con, _band_sql())
    (runs, days, stations, first, last) = con.execute(
        f"""
        {_runs_sql()}
        SELECT count(*), count(DISTINCT day), list(DISTINCT station_code ORDER BY station_code),
               min(day), max(day)
        FROM runs
        """
    ).fetchone()
    by_type = con.execute(
        f"""
        {_runs_sql()}
        SELECT train_type, count(*), median(delay), avg((delay >= {LATE_MINUTES})::INT)
        FROM runs GROUP BY ALL ORDER BY train_type
        """
    ).fetchall()
    return {
        "runs": int(runs),
        "days": int(days),
        "stations": stations or [],
        "first_day": first.isoformat() if first else None,
        "last_day": last.isoformat() if last else None,
        "min_runs": MIN_RUNS,
        "late_minutes": LATE_MINUTES,
        "close_minutes": CLOSE_MINUTES,
        "directions": [{"key": key, "label": label} for key, label in DIRECTIONS.items()],
        "bands": [
            {"key": key, "label": label, "from": start, "to": end}
            for key, label, start, end in BANDS
        ],
        "by_band": [asdict(cell) for cell in bands],
        "by_hour": [asdict(cell) | {"key": int(cell.key)} for cell in hours],
        "by_type": [
            {"type": kind, "runs": int(n), "median": float(med), "share_late": float(late)}
            for kind, n, med, late in by_type
        ],
    }


def format_report(data: dict, markdown: bool = False) -> str:
    """The /patterns page as text: a summary line, the part-of-day table, fast vs slow."""
    if not data["runs"]:
        return "No real readings yet: run `uv run sitt-collect` a few times, then try again."
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
    print(format_report(data, args.markdown))
    return 0 if data["runs"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
