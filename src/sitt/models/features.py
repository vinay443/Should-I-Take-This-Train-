"""Turn observations plus the timetable into rows a delay model can learn from.

A row asks: *knowing what was known at `cutoff`, how late will this train be at this
station?* The same code builds rows for training (where the answer is a later
observation) and for prediction (where there is no answer yet), so the two can't drift
apart:

    prepare(con)                          temporary tables: cleaned observations, etc.
    training_targets(con, start, end)     one target per (later observation, earlier cutoff)
    set_targets(con, targets)             or: targets for prediction
    build_features(con)                   -> columns as numpy arrays (see FEATURES)

A target's features use only observations made at or before its cutoff:

- the train and station, and where the station is on the route (`progress`, `point_seq`,
  `sched_minutes` since the train's first station, whether the train stops there);
- fast/slow, AC, car count, direction;
- the scheduled hour at the station, weekday, month, and whether the day runs to the
  Sunday timetable (a Sunday or a holiday in `sitt.holidays`);
- `lead_minutes`: how far ahead of the cutoff the train is due at the station;
- the trip's own latest reading before the cutoff: its delay, and how far back along the
  route it was (`prior_*`). Missing if the train hasn't been seen yet;
- the state of the line in the last collector batch before the cutoff, if it is no more
  than `STATE_MAX_AGE_MINUTES` old: the median delay of trains running the same way
  (`line_median_delay`), and the delay of the nearest train ahead on the same tracks
  (`ahead_delay`, `ahead_gap` stations away);
- `hist_delay`: the median delay of this train at this station on earlier days (and
  `hist_count`, how many readings that is). Never the same day, so a row can't see its
  own answer;
- whether a megablock from the `blocks` table covers the station at the scheduled time.
  A block recorded without times is assumed to run during `BlockSettings`' default
  hours (10:00-16:00 unless configured), not all day.

Training makes several rows per observation: one for each earlier reading of the same
trip (cutoff = that reading's time), and one "cold" row with the cutoff ten minutes
before the train starts, so the model learns to predict both with and without a live
reading.

All times here are naive Mumbai local timestamps.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

import duckdb
import numpy as np

from sitt.config import BlockSettings, load_block_settings
from sitt.holidays import FIXED_HOLIDAYS, MOVABLE_HOLIDAYS
from sitt.ingest.timetable import _stage
from sitt.routes import stage_route_points
from sitt.tz import IST

STATE_MAX_AGE_MINUTES = 20
COLD_CUTOFF_MINUTES = 10  # a cold row's cutoff is this long before the train starts
IST_OFFSET_MS = 19_800_000

CATEGORICAL = ("train_id", "station_code")
NUMERIC = (
    "progress",
    "point_seq",
    "is_stop",
    "is_fast",
    "is_ac",
    "car_count",
    "is_up",
    "hour",
    "weekday",
    "month",
    "sunday_schedule",
    "sched_minutes",
    "lead_minutes",
    "has_prior",
    "prior_delay",
    "prior_lead_minutes",
    "prior_points_back",
    "line_median_delay",
    "line_count",
    "ahead_delay",
    "ahead_gap",
    "hist_delay",
    "hist_count",
    "megablock",
)
FEATURES = (*CATEGORICAL, *NUMERIC)
# Carried alongside the features for splitting, baselines and reporting; never model inputs.
EXTRA = ("target_id", "service_day", "direction", "label", "label_time", "cutoff", "sched_time")


@dataclass(frozen=True)
class Target:
    """One prediction to make: this run of this train, at this station, as of `cutoff`."""

    train_id: str
    service_day: date
    station_code: str
    cutoff: datetime  # naive Mumbai local time, or aware


def local_naive(moment: datetime) -> datetime:
    """Mumbai wall-clock time without a time zone. Naive input is taken as already local."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(IST).replace(tzinfo=None)


def prepare(
    con: duckdb.DuckDBPyConnection,
    blocks: BlockSettings | None = None,
    exclude_flagged: bool = True,
) -> None:
    """Create the temporary tables the feature query reads. Call again after loading data.

    `blocks` says which hours a megablock without times is assumed to cover; by default
    it is read from the environment (`sitt.config.load_block_settings`).

    Observations with a row in `dq_flags` (suspect readings; see sitt.dq) are left out
    unless `exclude_flagged` is false. A database without that table has nothing flagged.
    """
    blocks = blocks or load_block_settings()
    has_flags = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'dq_flags'"
    ).fetchone()[0]
    not_flagged = (
        "AND o.id NOT IN (SELECT observation_id FROM dq_flags)"
        if exclude_flagged and has_flags
        else ""
    )
    stage_route_points(con)
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE run_info AS
        SELECT t.train_id, t.number, t.direction, t.train_type, t.line, t.is_ac, t.car_count,
               hour(s.run_start) * 60 + minute(s.run_start) AS run_start_min,
               o.station_seq AS origin_seq
        FROM trains t
        JOIN (SELECT train_id,
                     arg_min(coalesce(scheduled_departure, scheduled_arrival), stop_seq)
                         AS run_start
              FROM scheduled_stops GROUP BY train_id) s USING (train_id)
        JOIN route_points o ON o.train_id = t.train_id AND o.point_seq = 0
        """
    )
    # Readings that place a train at a station with a delay. The run's service day is
    # worked out from how long after the scheduled start the reading was taken.
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE obs AS
        WITH base AS (
            SELECT o.observed_at, o.train_id, o.station_code, o.delay_minutes, o.batch_id,
                   o.source,
                   make_timestamp((epoch_ms(o.observed_at) + {IST_OFFSET_MS}) * 1000) AS local_ts,
                   r.run_start_min, r.direction, r.train_type,
                   p.point_seq, p.station_seq, p.minutes AS sched_minutes
            FROM observations o
            JOIN run_info r USING (train_id)
            JOIN route_points p ON p.train_id = o.train_id AND p.station_code = o.station_code
            WHERE o.delay_minutes IS NOT NULL AND NOT coalesce(o.cancelled, false)
              AND coalesce(o.event, '') NOT IN ('cancelled', 'rake_at', 'unknown')
              {not_flagged}
        ), timed AS (
            SELECT *, ((hour(local_ts) * 60 + minute(local_ts) - run_start_min) % 1440 + 1440)
                      % 1440 AS since
            FROM base
        )
        SELECT * EXCLUDE (since),
               CAST(local_ts - to_minutes(CASE WHEN since > 720 THEN since - 1440 ELSE since END)
                    AS DATE) AS service_day
        FROM timed
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE batches AS
        SELECT batch_id, max(local_ts) AS batch_ts FROM obs
        WHERE batch_id IS NOT NULL GROUP BY batch_id
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE batch_line AS
        SELECT batch_id, direction, median(delay_minutes) AS line_median, count(*) AS line_count
        FROM obs WHERE batch_id IS NOT NULL GROUP BY batch_id, direction
        """
    )
    has_blocks = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'blocks'"
    ).fetchone()[0]
    blocks_from = "blocks" if has_blocks else "(SELECT NULL::DATE AS block_date WHERE false)"
    columns = (
        "b.block_date, b.line, b.tracks, "
        # A block announced without times: assume the usual megablock hours, not all day.
        "CASE WHEN b.start_time IS NULL AND b.end_time IS NULL THEN $start "
        "ELSE b.start_time END AS start_time, "
        "CASE WHEN b.start_time IS NULL AND b.end_time IS NULL THEN $end "
        "ELSE b.end_time END AS end_time, "
        "f.seq AS from_seq, t.seq AS to_seq"
        if has_blocks
        else "NULL::DATE AS block_date, NULL::VARCHAR AS line, NULL::VARCHAR AS tracks, "
        "NULL::TIME AS start_time, NULL::TIME AS end_time, NULL::INTEGER AS from_seq, "
        "NULL::INTEGER AS to_seq"
    )
    joins = (
        "LEFT JOIN stations f ON f.code = b.from_station "
        "LEFT JOIN stations t ON t.code = b.to_station"
        if has_blocks
        else ""
    )
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE block_spans AS "
        f"SELECT {columns} FROM {blocks_from} b {joins}",
        {"start": blocks.default_start, "end": blocks.default_end} if has_blocks else None,
    )
    (low, high) = con.execute("SELECT min(service_day), max(service_day) FROM obs").fetchone()
    today = date.today()
    years = range((low or today).year, max((high or today).year, today.year) + 2)
    days = {date(year, month, day) for year in years for month, day in FIXED_HOLIDAYS}
    days |= set(MOVABLE_HOLIDAYS)
    _stage(con, "holiday_dates", {"day": "DATE"}, [{"day": d.isoformat()} for d in sorted(days)])


def training_targets(
    con: duckdb.DuckDBPyConnection, start: date | None = None, end: date | None = None
) -> int:
    """Make the `targets` table from observations whose run falls in [start, end]."""
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE targets AS
        WITH labels AS (
            SELECT * FROM obs
            WHERE ($start IS NULL OR service_day >= $start)
              AND ($end IS NULL OR service_day <= $end)
        )
        SELECT row_number() OVER () AS target_id, * FROM (
            SELECT l.train_id, l.service_day, l.station_code, p.local_ts AS cutoff,
                   l.delay_minutes AS label, l.local_ts AS label_time
            FROM labels l
            JOIN obs p ON p.train_id = l.train_id AND p.service_day = l.service_day
                      AND p.local_ts < l.local_ts
            UNION ALL
            SELECT l.train_id, l.service_day, l.station_code,
                   CAST(l.service_day AS TIMESTAMP)
                       + to_minutes(r.run_start_min - {COLD_CUTOFF_MINUTES}) AS cutoff,
                   l.delay_minutes, l.local_ts
            FROM labels l JOIN run_info r USING (train_id)
        )
        """,
        {"start": start, "end": end},
    )
    return con.execute("SELECT count(*) FROM targets").fetchone()[0]


def set_targets(con: duckdb.DuckDBPyConnection, targets: Sequence[Target]) -> None:
    """Make the `targets` table for prediction: no labels."""
    rows = [
        {
            "target_id": i,
            "train_id": t.train_id,
            "service_day": t.service_day.isoformat(),
            "station_code": t.station_code,
            "cutoff": local_naive(t.cutoff).isoformat(sep=" "),
            "label": None,
            "label_time": None,
        }
        for i, t in enumerate(targets)
    ]
    _stage(
        con,
        "targets",
        {
            "target_id": "BIGINT",
            "train_id": "VARCHAR",
            "service_day": "DATE",
            "station_code": "VARCHAR",
            "cutoff": "TIMESTAMP",
            "label": "DOUBLE",
            "label_time": "TIMESTAMP",
        },
        rows,
    )


_FEATURE_SQL = f"""
CREATE OR REPLACE TEMP TABLE feature_rows AS
WITH t AS (
    SELECT g.*, r.direction, r.train_type, r.is_ac, r.car_count, r.line, r.origin_seq,
           p.point_seq, p.station_seq, p.minutes AS sched_minutes, p.is_stop, p.progress,
           CAST(g.service_day AS TIMESTAMP)
               + to_seconds(CAST((r.run_start_min + p.minutes) * 60 AS BIGINT)) AS sched_time
    FROM targets g
    JOIN run_info r USING (train_id)
    JOIN route_points p ON p.train_id = g.train_id AND p.station_code = g.station_code
), with_prior AS (
    SELECT t.*, o.delay_minutes AS prior_delay, o.sched_minutes AS prior_sched,
           o.point_seq AS prior_point, o.station_seq AS prior_station_seq
    FROM t ASOF LEFT JOIN obs o
      ON t.train_id = o.train_id AND t.service_day = o.service_day AND t.cutoff >= o.local_ts
), with_state AS (
    SELECT w.*,
           CASE WHEN date_diff('minute', b.batch_ts, w.cutoff) <= {STATE_MAX_AGE_MINUTES}
                THEN b.batch_id END AS state_batch,
           coalesce(w.prior_station_seq, w.origin_seq) AS our_seq
    FROM with_prior w ASOF LEFT JOIN batches b ON w.cutoff >= b.batch_ts
), history AS (
    -- Earlier days only, so a row never sees its own day's delays.
    SELECT k.train_id, k.station_code, k.service_day,
           median(o.delay_minutes) AS hist_delay, count(*) AS hist_count
    FROM (SELECT DISTINCT train_id, station_code, service_day FROM targets) k
    JOIN obs o ON o.train_id = k.train_id AND o.station_code = k.station_code
              AND o.service_day < k.service_day
    GROUP BY ALL
), ahead AS (
    SELECT s.target_id,
           arg_min(a.delay_minutes, abs(a.station_seq - s.our_seq)) AS ahead_delay,
           min(abs(a.station_seq - s.our_seq)) AS ahead_gap
    FROM with_state s
    JOIN obs a ON a.batch_id = s.state_batch AND a.direction = s.direction
              AND a.train_type = s.train_type AND a.train_id <> s.train_id
              AND CASE WHEN s.direction = 'down' THEN a.station_seq > s.our_seq
                       ELSE a.station_seq < s.our_seq END
    GROUP BY s.target_id
)
SELECT s.target_id, s.train_id, s.station_code, s.service_day, s.direction,
       s.label, s.label_time, s.cutoff, s.sched_time,
       s.progress, s.point_seq, CAST(s.is_stop AS INTEGER) AS is_stop,
       CAST(s.train_type = 'fast' AS INTEGER) AS is_fast,
       CAST(s.is_ac AS INTEGER) AS is_ac, s.car_count,
       CAST(s.direction = 'up' AS INTEGER) AS is_up,
       hour(s.sched_time) AS hour, isodow(s.service_day) - 1 AS weekday,
       month(s.service_day) AS month,
       CAST(isodow(s.service_day) = 7
            OR s.service_day IN (SELECT day FROM holiday_dates) AS INTEGER) AS sunday_schedule,
       s.sched_minutes,
       date_diff('second', s.cutoff, s.sched_time) / 60.0 AS lead_minutes,
       CAST(s.prior_delay IS NOT NULL AS INTEGER) AS has_prior,
       s.prior_delay,
       s.sched_minutes - s.prior_sched AS prior_lead_minutes,
       s.point_seq - s.prior_point AS prior_points_back,
       l.line_median AS line_median_delay, l.line_count,
       a.ahead_delay, a.ahead_gap,
       h.hist_delay, coalesce(h.hist_count, 0) AS hist_count,
       CAST(EXISTS (
           SELECT 1 FROM block_spans k
           WHERE k.block_date = s.service_day AND k.line = s.line
             AND (k.tracks IS NULL OR k.tracks = 'both' OR k.tracks = s.train_type)
             AND (k.from_seq IS NULL OR k.to_seq IS NULL
                  OR s.station_seq BETWEEN least(k.from_seq, k.to_seq)
                                       AND greatest(k.from_seq, k.to_seq))
             AND (k.start_time IS NULL OR k.end_time IS NULL
                  OR CASE WHEN k.start_time <= k.end_time
                          THEN CAST(s.sched_time AS TIME) BETWEEN k.start_time AND k.end_time
                          ELSE CAST(s.sched_time AS TIME) >= k.start_time
                               OR CAST(s.sched_time AS TIME) <= k.end_time END)
       ) AS INTEGER) AS megablock
FROM with_state s
LEFT JOIN batch_line l ON l.batch_id = s.state_batch AND l.direction = s.direction
LEFT JOIN ahead a USING (target_id)
LEFT JOIN history h ON h.train_id = s.train_id AND h.station_code = s.station_code
                   AND h.service_day = s.service_day
"""


def build_features(con: duckdb.DuckDBPyConnection) -> dict[str, np.ndarray]:
    """Compute features for the `targets` table. Returns one array per column, by target_id.

    Numeric columns are float arrays with NaN where a value is unknown. The rows are
    also left in the temporary table `feature_rows`.
    """
    con.execute(_FEATURE_SQL)
    columns = ", ".join((*EXTRA, *FEATURES))
    raw = con.execute(f"SELECT {columns} FROM feature_rows ORDER BY target_id").fetchnumpy()
    out: dict[str, np.ndarray] = {}
    for name, values in raw.items():
        if name in NUMERIC or name == "label":
            values = np.ma.filled(np.ma.asarray(values).astype(float), np.nan)
        elif isinstance(values, np.ma.MaskedArray):
            values = values.filled(None) if values.dtype == object else np.asarray(values)
        out[name] = values
    return out
