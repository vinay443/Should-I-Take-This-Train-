"""How late trains have really been: delay by hour and direction, from real readings.

Each *run* (one train at one station on one service day) counts once, with the delay of its
last reading, and only if that reading was taken close to the train's time: the same rule
as docs/live-drift.md (sitt.models.live_drift.runs_ctes). Counting readings instead would
weight a train by how many times the collector happened to see it, and early "on time"
readings understate delays.

Only real (not synthetic) readings of suburban trains are used. Small cells are kept but
marked, so the page can say how little a number rests on.
"""

from dataclasses import asdict, dataclass

import duckdb

from sitt.models.features import IST_OFFSET_MS
from sitt.models.live_drift import CLOSE_MINUTES, runs_ctes

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
    local = f"make_timestamp((epoch_ms(f.scheduled) + {IST_OFFSET_MS}) * 1000)"
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
