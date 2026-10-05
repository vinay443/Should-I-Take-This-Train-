"""Where the dashboard's data comes from, and the queries behind its pages.

No Streamlit imports here, so everything can be tested without a browser.

Choosing the data (`find_sources`)
----------------------------------
**Timetable.** The real database (`SITT_DB_PATH`, default `data/sitt.duckdb`) if it
holds a timetable. Otherwise a demo database is built in a cache folder: from Central
Railway's official PDFs if they can be downloaded (four requests, checked against the
known hashes), else from the invented sample timetable in `tests/fixtures`. That is
what makes the app deployable with nothing but the repository.

**Observations.** The real database's, if it has any. Otherwise synthetic ones: the
database `python -m sitt.synth` built (`data/synthetic.duckdb`) if it matches the
timetable in use, else a few weeks generated on the spot into the cache folder.
`Sources.synthetic` says which, and every page that shows observations must show a
banner when it is set.
"""

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import httpx
import pandas as pd

from sitt.config import load_settings
from sitt.db import init_db
from sitt.ingest import cr_pdf
from sitt.ingest.live.common import make_client
from sitt.ingest.timetable import load_timetable, read_timetable
from sitt.models.features import IST_OFFSET_MS, prepare
from sitt.synth import SOURCE as SYNTHETIC_SOURCE
from sitt.synth import build_database

REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE_TIMETABLE = REPO_ROOT / "tests" / "fixtures" / "sample_timetable.csv"
SYNTHETIC_DB = Path("data/synthetic.duckdb")
MODEL_RESULTS_DOC = REPO_ROOT / "docs" / "model-results.md"
DEMO_SYNTHETIC_WEEKS = 3
DEMO_SYNTHETIC_START = date(2026, 6, 1)

SYNTHETIC_BANNER = (
    "SYNTHETIC DATA. No real observations have been collected yet, so the delays shown here "
    "were invented by a generator (see docs/synthetic-data.md). They say nothing about real "
    "trains."
)
SAMPLE_BANNER = (
    "SAMPLE TIMETABLE. The real timetable isn't loaded and couldn't be downloaded, so this "
    "is a small invented timetable with made-up trains and times. Do not plan travel with it."
)

TIMETABLE_REAL, TIMETABLE_DOWNLOADED, TIMETABLE_SAMPLE = "real", "downloaded", "sample"


@dataclass(frozen=True)
class Sources:
    timetable_db: Path
    timetable_kind: str  # real | downloaded | sample
    observations_db: Path | None  # None when there are no observations at all
    synthetic: bool  # the observations are invented
    crowd_db: Path  # where crowd reports are (the real database)
    notes: tuple[str, ...] = field(default=())


def cache_dir() -> Path:
    """Folder for anything the dashboard builds for itself. Never inside `data/`."""
    folder = Path(
        os.environ.get("SITT_DASHBOARD_CACHE") or Path(tempfile.gettempdir()) / "sitt-dashboard"
    )
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _count(db: Path, table: str) -> int:
    if not db.exists():
        return 0
    try:
        with duckdb.connect(str(db), read_only=True) as con:
            return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    except duckdb.Error:
        return 0


def _train_count_matches(a: Path, b: Path) -> bool:
    return _count(a, "trains") == _count(b, "trains") > 0


def download_official_pdfs(folder: Path, client: httpx.Client | None = None) -> dict[str, Path]:
    """Fetch the four known timetable PDFs into `folder`. Raises if any fails its hash."""
    folder.mkdir(parents=True, exist_ok=True)
    paths: dict[str, list[Path]] = {"main": [], "ac": [], "15car": []}
    own = client is None
    client = client or make_client()
    try:
        for digest, (_, url, kind) in cr_pdf.KNOWN_SOURCES.items():
            target = folder / f"{kind}-{digest[:8]}.pdf"
            if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                response = client.get(url)
                response.raise_for_status()
                if hashlib.sha256(response.content).hexdigest() != digest:
                    raise ValueError(f"{url} no longer matches the edition this project knows")
                target.write_bytes(response.content)
            paths[kind].append(target)
    finally:
        if own:
            client.close()
    return {"main": paths["main"], "ac": paths["ac"][0], "15car": paths["15car"][0]}


def build_demo_timetable(folder: Path, allow_download: bool) -> tuple[Path, str, list[str]]:
    """Build (or reuse) a timetable database in `folder`. Returns (path, kind, notes)."""
    downloaded, sample = folder / "timetable-official.duckdb", folder / "timetable-sample.duckdb"
    if _count(downloaded, "trains"):
        return downloaded, TIMETABLE_DOWNLOADED, []
    notes = []
    if allow_download:
        try:
            pdfs = download_official_pdfs(folder / "pdfs")
            conversion, sources = cr_pdf.convert(
                pdfs["main"], ac_supplement=pdfs["ac"], cars_supplement=pdfs["15car"]
            )
            csv_path = folder / "central.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as out:
                cr_pdf.write_csv(out, conversion.trains, sources)
            with init_db(downloaded) as con:
                load_timetable(con, read_timetable(csv_path), replace=True)
            return downloaded, TIMETABLE_DOWNLOADED, []
        except Exception as exc:  # any failure falls back to the sample
            downloaded.unlink(missing_ok=True)
            notes.append(f"Couldn't build the timetable from Central Railway's PDFs ({exc}).")
    if not _count(sample, "trains"):
        with init_db(sample) as con:
            load_timetable(con, read_timetable(SAMPLE_TIMETABLE), replace=True)
    return sample, TIMETABLE_SAMPLE, notes


def find_sources(
    real_db: Path | None = None,
    synthetic_db: Path = SYNTHETIC_DB,
    cache: Path | None = None,
    allow_download: bool | None = None,
) -> Sources:
    """Decide which databases the dashboard reads, building demo data if needed."""
    real_db = real_db or load_settings().db_path
    cache = cache or cache_dir()
    if allow_download is None:
        allow_download = os.environ.get("SITT_DASHBOARD_DOWNLOAD", "1") != "0"
    notes: list[str] = []

    if _count(real_db, "scheduled_stops"):
        timetable_db, kind = real_db, TIMETABLE_REAL
    else:
        timetable_db, kind, notes = build_demo_timetable(cache, allow_download)

    if kind == TIMETABLE_REAL and _count(real_db, "observations"):
        observations_db = real_db
        with duckdb.connect(str(real_db), read_only=True) as con:
            sources = {
                r[0] for r in con.execute("SELECT DISTINCT source FROM observations").fetchall()
            }
        synthetic = SYNTHETIC_SOURCE in sources
    elif _count(synthetic_db, "observations") and _train_count_matches(synthetic_db, timetable_db):
        observations_db, synthetic = synthetic_db, True
    else:
        observations_db = cache / f"synthetic-{kind}.duckdb"
        if not (
            _count(observations_db, "observations")
            and _train_count_matches(observations_db, timetable_db)
        ):
            build_database(
                timetable_db,
                observations_db,
                cache / f"synthetic-{kind}",
                DEMO_SYNTHETIC_START,
                weeks=DEMO_SYNTHETIC_WEEKS,
            )
        synthetic = True
    return Sources(timetable_db, kind, observations_db, synthetic, real_db, tuple(notes))


# --- Queries ------------------------------------------------------------------------------


def _read(db: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db), read_only=True)


def stations(db: Path) -> pd.DataFrame:
    with _read(db) as con:
        return con.execute("SELECT code, name, seq FROM stations ORDER BY seq").df()


def timetable_summary(db: Path) -> dict:
    with _read(db) as con:
        row = con.execute(
            "SELECT count(*), count(*) FILTER (is_ac), count(*) FILTER (car_count = 15), "
            "count(*) FILTER (is_ladies_special), count(*) FILTER (train_type = 'fast') "
            "FROM trains"
        ).fetchone()
        (station_count,) = con.execute("SELECT count(*) FROM stations").fetchone()
    keys = ("trains", "ac", "fifteen_car", "ladies_special", "fast")
    return dict(zip(keys, row, strict=True)) | {"stations": station_count}


def observation_summary(db: Path) -> dict:
    with _read(db) as con:
        rows, first, last, cancelled = con.execute(
            f"SELECT count(*), "
            f"min(make_timestamp((epoch_ms(observed_at) + {IST_OFFSET_MS}) * 1000)), "
            f"max(make_timestamp((epoch_ms(observed_at) + {IST_OFFSET_MS}) * 1000)), "
            "count(*) FILTER (coalesce(cancelled, false)) FROM observations"
        ).fetchone()
        sources = [
            r[0]
            for r in con.execute("SELECT DISTINCT source FROM observations ORDER BY 1").fetchall()
        ]
    return {"rows": rows, "first": first, "last": last, "cancelled": cancelled, "sources": sources}


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def delay_by_hour_weekday(db: Path, direction: str | None = None) -> pd.DataFrame:
    """Mean delay per scheduled hour and weekday. Columns: weekday, hour, delay, readings."""
    with _read(db) as con:
        prepare(con)
        frame = con.execute(
            """
            SELECT isodow(service_day) - 1 AS weekday_index, hour(local_ts) AS hour,
                   avg(delay_minutes) AS delay, count(*) AS readings
            FROM obs WHERE $direction IS NULL OR direction = $direction
            GROUP BY ALL ORDER BY ALL
            """,
            {"direction": direction},
        ).df()
    frame["weekday"] = frame["weekday_index"].map(lambda i: WEEKDAYS[int(i)])
    return frame


def delay_by_station(db: Path, direction: str) -> pd.DataFrame:
    """Mean delay per station and hour, one direction. Stations in line order."""
    with _read(db) as con:
        prepare(con)
        return con.execute(
            """
            SELECT s.name AS station, s.seq, hour(o.local_ts) AS hour,
                   avg(o.delay_minutes) AS delay, count(*) AS readings
            FROM obs o JOIN stations s ON s.code = o.station_code
            WHERE o.direction = $direction
            GROUP BY ALL ORDER BY s.seq, hour
            """,
            {"direction": direction},
        ).df()


def delay_by_train(db: Path) -> pd.DataFrame:
    """Per train: typical and bad-day delay, and how often it was reported cancelled."""
    with _read(db) as con:
        prepare(con)
        return con.execute(
            """
            WITH runs AS (
                SELECT train_id, count(DISTINCT service_day) AS days,
                       median(delay_minutes) AS median_delay,
                       quantile_cont(delay_minutes, 0.9) AS p90_delay,
                       max(delay_minutes) AS worst_delay, count(*) AS readings
                FROM obs GROUP BY train_id
            ), cancelled AS (
                SELECT train_id, count(DISTINCT CAST(observed_at AS DATE)) AS cancelled_days
                FROM observations WHERE coalesce(cancelled, false) GROUP BY train_id
            )
            SELECT t.number, t.service_code, t.train_type, t.direction, t.label AS destination,
                   strftime(CAST('2000-01-01' AS DATE) + first_stop.departs, '%H:%M') AS departs,
                   r.median_delay, r.p90_delay, r.worst_delay, r.days, r.readings,
                   coalesce(c.cancelled_days, 0) AS cancelled_days
            FROM runs r JOIN trains t USING (train_id)
            JOIN (SELECT train_id,
                         arg_min(coalesce(scheduled_departure, scheduled_arrival), stop_seq)
                             AS departs
                  FROM scheduled_stops GROUP BY train_id) first_stop USING (train_id)
            LEFT JOIN cancelled c USING (train_id)
            ORDER BY r.median_delay DESC, t.number
            """
        ).df()


def delay_along_route(db: Path, number: str) -> pd.DataFrame:
    """One train's delay at each station it was seen at, in route order."""
    with _read(db) as con:
        prepare(con)
        return con.execute(
            """
            SELECT s.name AS station, o.point_seq, median(o.delay_minutes) AS median_delay,
                   quantile_cont(o.delay_minutes, 0.9) AS p90_delay, count(*) AS readings
            FROM obs o JOIN trains t USING (train_id) JOIN stations s ON s.code = o.station_code
            WHERE t.number = ? GROUP BY ALL ORDER BY o.point_seq
            """,
            [number],
        ).df()


def crowd_reports(db: Path) -> pd.DataFrame:
    if not _count(db, "crowd_reports"):
        return pd.DataFrame(columns=["reported", "station_code", "train", "crowd_level", "note"])
    with _read(db) as con:
        return con.execute(
            f"""
            SELECT make_timestamp((epoch_ms(reported_at) + {IST_OFFSET_MS}) * 1000) AS reported,
                   station_code, train_description AS train, crowd_level, note
            FROM crowd_reports ORDER BY reported_at DESC
            """
        ).df()


def load_model_metadata(model_dir: Path) -> dict | None:
    path = Path(model_dir) / "metadata.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def default_moment(sources: Sources, now: datetime) -> datetime:
    """A sensible time for the pickers: now, or a weekday morning for the sample timetable."""
    if sources.timetable_kind == TIMETABLE_SAMPLE:
        monday = now - timedelta(days=now.weekday())
        return monday.replace(hour=7, minute=0, second=0, microsecond=0)
    return now.replace(second=0, microsecond=0)
