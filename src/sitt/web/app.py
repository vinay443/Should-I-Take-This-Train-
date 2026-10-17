"""The web app: a small JSON API over the recommender, and the page that uses it.

    uv run sitt-web                 http://127.0.0.1:8000, opens your browser

Endpoints (all JSON):

    GET  /api/status                what data is loaded, when it was last collected
    GET  /api/stations              every station in the timetable, in line order
    GET  /api/recommend?from=KYN&to=CSMT[&at=08:00][&date=2026-10-14][&train=08:12][&n=5]
    POST /api/collect               one live collection round, only when asked (never scheduled)
    GET  /api/patterns              delay by hour and direction, from real readings (/patterns)
    GET  /api/reports               the latest crowd reports (from the page or the bot)
    POST /api/reports               log how crowded a train was
    DELETE /api/reports/{id}        take back a report logged from the page in the last
                                    UNDO_MINUTES minutes (a mis-tap)

Also /manifest.webmanifest and /sw.js, so a phone can keep the page on its home screen and
open it when the PC can't be reached (see docs/web.md).

It reads the same database as the bot and `sitt-next` (SITT_DB_PATH). DuckDB lets one
process write at a time, so every request takes a short turn: requests in this app queue
behind one lock, and while another process (the bot, the collector) has the file the app
waits a few seconds and then answers 503 "busy, try again".
"""

import contextlib
import hashlib
import json
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from sitt import matching, next_cli, patterns
from sitt.bot.formatting import trip_tags
from sitt.bot.parsing import parse_time
from sitt.bot.schedule import ScheduleError, find_station
from sitt.bot.stations import directory_from
from sitt.config import load_health_settings, load_recommend_settings, load_settings
from sitt.db import DatabaseBusyError, open_with_retry
from sitt.ingest.live import mobond
from sitt.models.crowding import CROWD_LABELS
from sitt.recommend import LEVEL_TIMETABLE, Option, Recommendation, explain, recommend
from sitt.tz import IST

STATIC = Path(__file__).parent / "static"
WEB_SOURCE = "web"  # crowd_reports.source for reports logged from the page
# A report logged from the page can be taken back for this long: long enough to notice a
# mis-tap, short enough that older reports (and the bot's) are never removed from here.
UNDO_MINUTES = 15
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class ReportIn(BaseModel):
    station: str = Field(min_length=1, max_length=60)
    departure: str = Field(min_length=3, max_length=8, description="HH:MM")
    service: str = Field(pattern="^(fast|slow)$")
    crowd: int = Field(ge=1, le=5)
    note: str | None = Field(default=None, max_length=500)


def _clock(text: str | None, name: str) -> time | None:
    if not text:
        return None
    parsed = parse_time(text)
    if parsed is None:
        raise HTTPException(400, f"{name} must be a time like 08:12, not {text!r}.")
    return parsed


# How far from today a question may be about. The timetable repeats weekly, so more than this
# says nothing new, and far-off dates overflow date arithmetic.
MAX_DAYS_AWAY = 400


def _day(text: str | None, today: date) -> date | None:
    if not text:
        return None
    try:
        day = date.fromisoformat(text)
    except ValueError:
        raise HTTPException(400, f"date must look like 2026-10-14, not {text!r}.") from None
    if abs((day - today).days) > MAX_DAYS_AWAY:
        raise HTTPException(400, f"Pick a date within a year of today, not {text}.")
    return day


def _iso(moment: datetime) -> str:
    """Mumbai wall-clock time as ISO text (the recommender works in naive local time)."""
    return moment.replace(tzinfo=IST).isoformat(timespec="minutes")


def option_json(option: Option, asked: datetime) -> dict:
    trip = option.trip
    crowd = option.crowding
    return {
        "train_id": trip.train_id,
        "number": trip.number,
        "type": trip.train_type,
        "destination": trip.label,
        "direction": trip.direction,
        "departs": _iso(trip.departure),
        "arrives": _iso(trip.arrival),
        "predicted_departure": _iso(option.predicted_departure),
        "predicted_arrival": _iso(option.predicted_arrival),
        "departure_delay": round(option.departure_delay),
        "arrival_delay": round(option.arrival_delay),
        "margin": option.arrival_margin,
        "minutes": round(trip.duration.total_seconds() / 60),
        "other_day": trip.departure.date() != asked.date(),
        "cancelled": option.cancelled,
        "starts_here": option.starts_here,
        "cars": trip.car_count or 12,
        "ac": bool(trip.runs_ac),
        "ladies_special": bool(trip.is_ladies_special),
        "tags": trip_tags(trip),
        "crowding": None
        if crowd is None
        else {"score": crowd.score, "label": crowd.label, "reason": crowd.reason},
        "live": None
        if option.live is None
        else {
            "delay": round(option.live.delay_minutes),
            "at": _iso(option.live.observed_at),
            "station": option.live.station_code,
            "source": option.live.source,
        },
    }


def recommendation_json(rec: Recommendation, focus: int | None = None) -> dict:
    return {
        "origin": rec.origin,
        "destination": rec.destination,
        "asked_at": _iso(rec.asked_at),
        "reason": rec.reason,
        "rule": rec.rule,
        "choice": rec.choice,
        "focus": focus,
        "level": rec.level,
        "level_text": rec.level_text,
        "timetable_only": rec.level == LEVEL_TIMETABLE
        and not any(option.live for option in rec.options),
        "synthetic": rec.synthetic,
        "notes": rec.notes,
        "explanation": explain(rec),
        "options": [option_json(option, rec.asked_at) for option in rec.options],
    }


def create_app(
    db_path: Path | str | None = None,
    model_dir: Path | str | None = None,
    data_dir: Path | str | None = None,
    now=None,
    collect=None,
) -> FastAPI:
    """The app. `now` and `collect` can be replaced in tests."""
    settings = load_settings()
    db = Path(db_path or settings.db_path)
    models = model_dir if model_dir is not None else settings.model_dir
    batches = Path(data_dir) if data_dir else db.parent
    clock = now or (lambda: datetime.now(IST))

    app = FastAPI(title="Should I take this train?", docs_url="/api/docs", redoc_url=None)

    # One connection at a time in this process: DuckDB refuses a read-write connection while
    # a read-only one to the same file is open in the same process, and the other way round.
    lock = threading.Lock()

    @contextlib.contextmanager
    def database(write: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
        if not db.exists():
            raise HTTPException(
                503, f"No database at {db}. Run `uv run sitt-init-db` and load a timetable."
            )
        with lock:
            try:
                con = open_with_retry(db, read_only=not write, attempts=4, wait_seconds=0.5)
            except DatabaseBusyError:
                raise HTTPException(
                    503,
                    "The database is busy (the bot or the collector is using it). "
                    "Try again in a moment.",
                ) from None
            try:
                yield con
            finally:
                con.close()

    def read():
        return database(write=False)

    @app.get("/api/status")
    def status() -> dict:
        if not db.exists():
            return {"database": False, "trains": 0, "stations": 0}
        with read() as con:
            trains, stations = con.execute(
                "SELECT (SELECT count(*) FROM trains), (SELECT count(*) FROM stations)"
            ).fetchone()
            real, synthetic = con.execute(
                "SELECT count(*) FILTER (source <> 'synthetic'), "
                "count(*) FILTER (source = 'synthetic') FROM observations"
            ).fetchone()
            # The invented sample timetable (tests/fixtures) numbers its trains 901xx.
            invented = con.execute(
                "SELECT count(*) FROM trains WHERE number LIKE '901%'"
            ).fetchone()[0]
            last = _last_collected(con) if real else None
        return {
            "database": True,
            "trains": trains,
            "stations": stations,
            "sample_timetable": bool(trains) and invented == trains,
            "observations": real,
            "synthetic_observations": synthetic,
            "last_collected": _iso(last) if last else None,
            "manual_collection": load_health_settings().cadence_minutes <= 0,
            "mobond_enabled": mobond.SOURCE in _enabled_sources(),
            "model": Path(models, "metadata.json").exists() if models else False,
        }

    @app.get("/api/stations")
    def stations() -> list[dict]:
        with read() as con:
            rows = con.execute("SELECT code, name, seq FROM stations ORDER BY seq, code").fetchall()
        return [{"code": code, "name": name, "seq": seq} for code, name, seq in rows]

    @app.get("/api/recommend")
    def recommend_route(
        origin: str = Query(alias="from", min_length=1),
        destination: str = Query(alias="to", min_length=1),
        at: str | None = None,
        day: str | None = Query(default=None, alias="date"),
        train: str | None = None,
        n: int = Query(default=5, ge=1, le=12),
    ) -> dict:
        now_ = clock()
        asked = next_cli.asked_at(
            now_, _clock(at, "at"), _day(day, now_.date()), _clock(train, "train")
        )
        recommend_settings = replace(load_recommend_settings(), candidates=n)
        with read() as con:
            if not con.execute("SELECT count(*) FROM scheduled_stops").fetchone()[0]:
                raise HTTPException(503, "The timetable isn't loaded yet.")
            directory = directory_from(con)
            try:
                start = find_station(directory, origin)
                end = find_station(directory, destination)
            except ScheduleError as exc:
                raise HTTPException(400, str(exc)) from None
            if start == end:
                raise HTTPException(400, "Pick two different stations.")
            rec = recommend(con, start.code, end.code, asked, recommend_settings, model_dir=models)
        focus = None
        if train:
            focus = next_cli.find_train(rec, _clock(train, "train"))
            if focus is None:
                raise HTTPException(
                    404,
                    f"No train leaves {rec.origin} at {train} for {rec.destination}. "
                    f"Trains around then: {', '.join(o.name for o in rec.options) or 'none'}.",
                )
        return recommendation_json(rec, focus) | {
            "origin_code": start.code,
            "destination_code": end.code,
        }

    @app.post("/api/collect")
    def collect_now() -> dict:
        """One collection round, because the rider pressed the button. Never scheduled."""
        if not db.exists():
            raise HTTPException(503, f"No database at {db}.")
        run = collect or _collect
        with lock:  # the collector opens the database itself
            return {"message": run(db, batches)}

    @app.get("/api/patterns")
    def delay_patterns() -> dict:
        """How late trains have really been, by hour and direction (real readings only)."""
        with read() as con:
            data = patterns.delay_patterns(con)
            if data["runs"]:  # how far these readings are from a real delay model
                data["model"] = patterns.model_progress(con)
            return data

    @app.get("/api/reports")
    def reports(limit: int = Query(default=10, ge=1, le=50)) -> list[dict]:
        with read() as con:
            rows = con.execute(
                "SELECT id, epoch_us(reported_at), station_code, train_description, "
                "crowd_level, matched_train_id, source FROM crowd_reports "
                "ORDER BY reported_at DESC, id DESC LIMIT ?",
                [limit],
            ).fetchall()
        now = datetime.now(UTC)
        return [
            {
                "id": report_id,
                "reported_at": (_EPOCH + timedelta(microseconds=micros))
                .astimezone(IST)
                .isoformat(timespec="minutes"),
                "station": station,
                "train": train,
                "crowd": level,
                "crowd_label": CROWD_LABELS[level],
                "matched": matched is not None,
                "can_undo": _undoable(source, _EPOCH + timedelta(microseconds=micros), now),
            }
            for report_id, micros, station, train, level, matched, source in rows
        ]

    @app.delete("/api/reports/{report_id}", status_code=204)
    def undo_report(report_id: int) -> Response:
        """Take back a report just logged from the page; nothing else can be removed here."""
        with database(write=True) as con:
            row = con.execute(
                "SELECT source, epoch_us(reported_at) FROM crowd_reports WHERE id = ?",
                [report_id],
            ).fetchone()
            if row is None:
                raise HTTPException(404, "No such report: it may have been taken back already.")
            source, micros = row
            if not _undoable(source, _EPOCH + timedelta(microseconds=micros), datetime.now(UTC)):
                raise HTTPException(
                    409,
                    f"Only a report logged from this page in the last {UNDO_MINUTES} minutes "
                    "can be taken back.",
                )
            con.execute("DELETE FROM crowd_reports WHERE id = ?", [report_id])
        return Response(status_code=204)

    @app.post("/api/reports", status_code=201)
    def log_report(report: ReportIn) -> dict:
        departure = _clock(report.departure, "departure")
        with read() as con:
            try:
                station = find_station(directory_from(con), report.station)
            except ScheduleError as exc:
                raise HTTPException(400, str(exc)) from None
        description = f"{departure:%H:%M} {report.service} from {station.code}"
        with database(write=True) as con:
            (report_id,) = con.execute(
                "INSERT INTO crowd_reports "
                "(reported_at, station_code, train_description, crowd_level, source, note) "
                "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                [
                    datetime.now(UTC),
                    station.code,
                    description,
                    report.crowd,
                    WEB_SOURCE,
                    report.note,
                ],
            ).fetchone()
        matched = None
        try:
            with database(write=True) as con:
                result = matching.match_stored(con, report_id, matching.AUTO, settings.match)
            if result is not None and result.train is not None:
                matched = result.train.describe()
        except Exception:  # the report is saved either way; sitt-match-logs can redo this
            matched = None
        return {
            "id": report_id,
            "train": description,
            "crowd_label": CROWD_LABELS[report.crowd],
            "matched": matched,
        }

    @app.exception_handler(HTTPException)
    async def http_error(request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc: RequestValidationError):
        problems = []
        for error in exc.errors():
            field = ".".join(
                str(part) for part in error.get("loc", ()) if part not in ("body", "query")
            )
            problems.append(f"{field or 'request'}: {error.get('msg', 'invalid')}")
        return JSONResponse({"error": "; ".join(problems) + "."}, status_code=422)

    # Worked out once, when the app starts: restart it after changing the page.
    version = static_version()
    pages = {name: page_html(name, version) for name in ("index.html", "patterns.html")}

    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        return HTMLResponse(pages["index.html"], headers={"Cache-Control": "no-cache"})

    @app.get("/patterns", include_in_schema=False)
    def patterns_page() -> HTMLResponse:
        return HTMLResponse(pages["patterns.html"], headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(
            STATIC / "manifest.webmanifest",
            media_type="application/manifest+json",
            headers={"Cache-Control": "no-cache"},
        )

    # The service worker must be served from the root to look after the whole page.
    worker = service_worker(version)

    @app.get("/sw.js", include_in_schema=False)
    def sw() -> Response:
        return Response(worker, media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


# Pages the service worker keeps, and the styles and scripts they load. The pages load
# these with ?v=<version>, so a new version of the page never runs an old cached script.
SHELL_PAGES = ("/", "/patterns")
VERSIONED = ("/static/styles.css", "/static/app.js", "/static/patterns.js")


def _undoable(source: str, reported_at: datetime, now: datetime) -> bool:
    """Whether a report can be taken back from the page: logged there, minutes ago."""
    return source == WEB_SOURCE and timedelta(0) <= now - reported_at <= timedelta(
        minutes=UNDO_MINUTES
    )


def static_version() -> str:
    """A hash of every static file: it changes whenever any part of the page does."""
    digest = hashlib.sha256()
    for path in sorted(STATIC.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(STATIC).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def page_html(name: str, version: str) -> str:
    """A page with its styles and scripts linked as `?v=<version>`."""
    html = (STATIC / name).read_text("utf-8")
    for url in VERSIONED:
        html = html.replace(f'"{url}"', f'"{url}?v={version}"')
    return html


def shell_files(version: str = "") -> list[str]:
    """What the service worker keeps so the page opens without the app: the pages, their
    styles and scripts (as the versioned URLs the pages use), fonts, manifest and icons."""
    suffix = f"?v={version}" if version else ""
    urls = [*SHELL_PAGES, "/manifest.webmanifest", *(url + suffix for url in VERSIONED)]
    for folder in ("fonts", "icons"):
        for path in sorted((STATIC / folder).iterdir()):
            if path.suffix in (".woff2", ".png"):
                urls.append(f"/static/{folder}/{path.name}")
    return urls


def service_worker(version: str | None = None) -> str:
    """sw.js with its cache version (so any change to the page makes phones fetch the new
    one), the pages it keeps and the list of files to keep."""
    version = version or static_version()
    source = (STATIC / "sw.js").read_text("utf-8")
    return (
        source.replace("__VERSION__", version)
        .replace("__PAGES__", json.dumps(list(SHELL_PAGES)))
        .replace("__SHELL__", json.dumps(shell_files(version)))
    )


def _last_collected(con: duckdb.DuckDBPyConnection) -> datetime | None:
    """The newest real reading or collector run, in Mumbai time (naive)."""
    from sitt.models.features import IST_OFFSET_MS

    (reading,) = con.execute(
        f"SELECT max(make_timestamp((epoch_ms(observed_at) + {IST_OFFSET_MS}) * 1000)) "
        "FROM observations WHERE source <> 'synthetic'"
    ).fetchone()
    has_runs = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'collector_runs'"
    ).fetchone()[0]
    run = None
    if has_runs:
        (run,) = con.execute(
            f"SELECT max(make_timestamp((epoch_ms(started_at) + {IST_OFFSET_MS}) * 1000)) "
            "FROM collector_runs"
        ).fetchone()
    found = [moment for moment in (reading, run) if moment is not None]
    return max(found) if found else None


def _enabled_sources() -> list[str]:
    from sitt.ingest.live import local

    return local.enabled_sources()


def _collect(db: Path, data_dir: Path) -> str:
    from sitt.ingest.live import local

    return local.collect_now(db, data_dir)
