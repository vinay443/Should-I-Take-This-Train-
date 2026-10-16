"""The dashboard's data layer, and a headless run of every page. No network."""

import tomllib
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from conftest import SAMPLE_CSV
from streamlit.testing.v1 import AppTest

from sitt.bot import storage
from sitt.dashboard import data
from sitt.db import init_db
from sitt.ingest import cr_pdf
from sitt.ingest.timetable import load_timetable, read_timetable

REPO = Path(__file__).parents[2]
APP = REPO / "src" / "sitt" / "dashboard" / "app.py"


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    """Sources for a deployment with nothing but the repository, and no network."""
    folder = tmp_path_factory.mktemp("dashboard")
    sources = data.find_sources(
        real_db=folder / "missing.duckdb",
        synthetic_db=folder / "none.duckdb",
        cache=folder / "cache",
        allow_download=False,
    )
    return folder, sources


def test_without_any_data_it_falls_back_to_the_sample_and_synthetic(demo):
    folder, sources = demo
    assert sources.timetable_kind == data.TIMETABLE_SAMPLE
    assert sources.synthetic is True
    assert sources.timetable_db.parent == folder / "cache"
    assert sources.observations_db.name == "synthetic-sample.duckdb"
    assert not (folder / "missing.duckdb").exists()  # the real database is never created
    # Asking again reuses what was built.
    stamp = sources.observations_db.stat().st_mtime
    again = data.find_sources(
        folder / "missing.duckdb", folder / "none.duckdb", folder / "cache", allow_download=False
    )
    assert again == sources and again.observations_db.stat().st_mtime == stamp


def test_real_timetable_and_observations_are_preferred(tmp_path):
    real = tmp_path / "sitt.duckdb"
    with init_db(real) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
    cache = tmp_path / "cache"
    no_observations = data.find_sources(real, tmp_path / "none.duckdb", cache, allow_download=False)
    assert (no_observations.timetable_kind, no_observations.timetable_db) == ("real", real)
    assert no_observations.synthetic and no_observations.observations_db != real

    with init_db(real) as con:
        con.execute(
            "INSERT INTO observations (observed_at, train_id, station_code, delay_minutes, source)"
            " VALUES (now(), 'central-90104', 'KYN', 2, 'ntes')"
        )
    collected = data.find_sources(real, tmp_path / "none.duckdb", cache, allow_download=False)
    assert (collected.observations_db, collected.synthetic) == (real, False)


def test_a_synthetic_database_for_another_timetable_is_not_used(demo, tmp_path):
    _, sources = demo
    other = tmp_path / "other-synthetic.duckdb"
    with init_db(other) as con:  # observations, but for a timetable with no trains
        con.execute(
            "INSERT INTO observations (observed_at, train_id, station_code, source) "
            "VALUES (now(), 'x', 'KYN', 'synthetic')"
        )
    found = data.find_sources(tmp_path / "missing.duckdb", other, tmp_path / "c", False)
    assert found.observations_db != other


def test_download_is_checked_against_known_hashes(tmp_path, monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"not the real pdf")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ValueError, match="no longer matches the edition"),
    ):
        data.download_official_pdfs(tmp_path, client)
    assert len(calls) == 1 and calls[0] in {url for _, url, _ in cr_pdf.KNOWN_SOURCES.values()}
    assert list(tmp_path.glob("*.pdf")) == []  # nothing unverified is kept

    # A failed download falls back to the sample timetable, with a note saying why.
    def fail(folder, client=None):
        raise httpx.ConnectError("blocked")

    monkeypatch.setattr(data, "download_official_pdfs", fail)
    path, kind, notes = data.build_demo_timetable(tmp_path / "cache", allow_download=True)
    assert kind == data.TIMETABLE_SAMPLE and path.name == "timetable-sample.duckdb"
    assert notes == ["Couldn't build the timetable from Central Railway's PDFs (blocked)."]


def test_queries(demo):
    _, sources = demo
    stations = data.stations(sources.timetable_db)
    assert list(stations["code"])[:2] == ["CSMT", "MSD"] and len(stations) == 26
    summary = data.timetable_summary(sources.timetable_db)
    assert (summary["trains"], summary["stations"]) == (13, 26)

    db = sources.observations_db
    observed = data.observation_summary(db)
    assert observed["sources"] == ["synthetic"] and observed["rows"] > 500
    assert observed["first"] < observed["last"]

    by_hour = data.delay_by_hour_weekday(db, "up")
    assert set(by_hour.columns) >= {"weekday", "hour", "delay", "readings"}
    assert set(by_hour["weekday"]) <= set(data.WEEKDAYS) and by_hour["hour"].between(0, 23).all()
    assert len(data.delay_by_hour_weekday(db)) >= len(by_hour)

    by_station = data.delay_by_station(db, "up")
    assert "Kalyan" in set(by_station["station"]) and by_station["seq"].is_monotonic_increasing

    trains = data.delay_by_train(db)
    assert len(trains) == 13 and trains["median_delay"].notna().all()
    assert set(trains.columns) >= {"number", "departs", "p90_delay", "cancelled_days"}
    route = data.delay_along_route(db, "90104")
    assert len(route) > 3 and route["point_seq"].is_monotonic_increasing


def test_crowd_reports(tmp_path):
    db = tmp_path / "test.duckdb"
    assert data.crowd_reports(db).empty  # no database yet
    init_db(db).close()
    assert data.crowd_reports(db).empty
    storage.insert_report(
        db,
        storage.NewReport(
            1, datetime(2026, 10, 5, 3, 0, tzinfo=UTC), "KYN", "08:12 fast from KYN", 4, "x"
        ),
    )
    reports = data.crowd_reports(db)
    assert len(reports) == 1 and reports.iloc[0]["crowd_level"] == 4
    assert reports.iloc[0]["reported"].hour == 8  # shown in Mumbai time


def test_model_metadata(tmp_path):
    assert data.load_model_metadata(tmp_path) is None
    (tmp_path / "metadata.json").write_text("{broken", encoding="utf-8")
    assert data.load_model_metadata(tmp_path) is None
    (tmp_path / "metadata.json").write_text('{"synthetic": true}', encoding="utf-8")
    assert data.load_model_metadata(tmp_path) == {"synthetic": True}


@pytest.fixture
def app(demo, tmp_path, monkeypatch):
    """The Streamlit app, pointed at the demo data with no real database or model."""
    folder, _ = demo
    monkeypatch.setenv("SITT_DB_PATH", str(folder / "missing.duckdb"))
    monkeypatch.setenv("SITT_DASHBOARD_CACHE", str(folder / "cache"))
    monkeypatch.setenv("SITT_DASHBOARD_DOWNLOAD", "0")
    monkeypatch.setenv("SITT_MODEL_DIR", str(tmp_path / "no-model"))
    monkeypatch.chdir(tmp_path)  # so data/synthetic.duckdb isn't found
    return AppTest.from_file(str(APP), default_timeout=120)


def _texts(at: AppTest) -> str:
    parts = [e.value for group in (at.header, at.subheader, at.caption, at.info) for e in group]
    parts += [e.value for group in (at.warning, at.error, at.markdown, at.text) for e in group]
    return "\n".join(str(p) for p in parts)


def test_every_page_runs_and_labels_what_is_not_real(app):
    at = app.run()
    assert not at.exception
    assert [o for o in at.sidebar.radio[0].options] == [
        "Timetable",
        "Delay patterns",
        "Crowd reports",
        "Recommender",
        "Model metrics",
    ]
    text = _texts(at)
    assert "Timetable explorer" in text and "SAMPLE TIMETABLE" in text
    assert len(at.dataframe) == 1 and len(at.dataframe[0].value) > 0

    at.sidebar.radio[0].set_value("Delay patterns").run()
    assert not at.exception
    assert "SYNTHETIC DATA" in _texts(at) and "sources: synthetic" in _texts(at)

    at.sidebar.radio[0].set_value("Crowd reports").run()
    assert not at.exception and "No crowd reports yet" in _texts(at)

    at.sidebar.radio[0].set_value("Recommender").run()
    assert not at.exception
    text = _texts(at)
    assert "Take the" in text or "Wait for the" in text
    assert "Predictions used: timetable only" in text and "SYNTHETIC predictions" not in text
    # Ticking "use observations" brings in the synthetic history, and the page says so.
    at.checkbox[1].check().run()
    assert not at.exception
    assert "SYNTHETIC predictions" in _texts(at) and "typical past delay" in _texts(at)

    at.sidebar.radio[0].set_value("Model metrics").run()
    assert not at.exception
    text = _texts(at)
    assert "No trained model in this deployment" in text and "SYNTHETIC RESULTS" in text


def test_last_collected_and_age_text(tmp_path):
    db = tmp_path / "sitt.duckdb"
    with init_db(db) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
    assert data.last_collected(db) is None
    with init_db(db) as con:
        con.execute(
            "INSERT INTO observations (observed_at, train_id, station_code, delay_minutes, source)"
            " VALUES (TIMESTAMPTZ '2026-10-06 07:15:00+00', 'central-90104', 'KYN', 2, 'ntes'),"
            " (TIMESTAMPTZ '2026-10-09 07:15:00+00', 'central-90104', 'KYN', 2, 'synthetic')"
        )
    # Synthetic readings don't count; times are Mumbai time.
    assert data.last_collected(db) == datetime(2026, 10, 6, 12, 45)
    now = datetime(2026, 10, 12, 19, 0)
    assert data.age_text(datetime(2026, 10, 12, 18, 30), now) == "30 min ago"
    assert data.age_text(datetime(2026, 10, 12, 14, 0), now) == "5 h ago"
    assert data.age_text(datetime(2026, 10, 6, 12, 45), now) == "6 days ago"


def test_the_sidebar_says_when_real_data_was_last_collected(tmp_path, monkeypatch):
    db = tmp_path / "sitt.duckdb"
    with init_db(db) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
        con.execute(
            "INSERT INTO observations (observed_at, train_id, station_code, delay_minutes, source)"
            " VALUES (TIMESTAMPTZ '2026-10-06 07:15:00+00', 'central-90104', 'KYN', 2, 'ntes')"
        )
    monkeypatch.setenv("SITT_DB_PATH", str(db))
    monkeypatch.setenv("SITT_DASHBOARD_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("SITT_DASHBOARD_DOWNLOAD", "0")
    monkeypatch.setenv("SITT_MODEL_DIR", str(tmp_path / "no-model"))
    monkeypatch.chdir(tmp_path)
    import streamlit as st

    st.cache_resource.clear()  # another test's data sources may be cached in this process
    st.cache_data.clear()
    at = AppTest.from_file(str(APP), default_timeout=120).run()
    assert not at.exception
    captions = "\n".join(c.value for c in at.sidebar.caption)
    assert "Observations: collected." in captions
    assert "Last collected 06 Oct 12:45" in captions and "--collect" in captions
    st.cache_resource.clear()  # don't leave these sources for the next app test
    st.cache_data.clear()


def test_requirements_txt_covers_the_projects_dependencies():
    """Streamlit Community Cloud installs requirements.txt, so it must not fall behind."""
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    required = (REPO / "requirements.txt").read_text(encoding="utf-8").lower()
    pinned = {line.split("==")[0].strip() for line in required.splitlines() if "==" in line}
    for dependency in project["dependencies"]:
        name = dependency.split(">=")[0].split("==")[0].strip().lower()
        name, _, extras = name.partition("[")  # e.g. python-telegram-bot[job-queue]
        assert name in pinned, f"{name} is missing from requirements.txt; run `uv export`"
        if "job-queue" in extras:
            assert "apscheduler" in pinned, "the job-queue extra is missing; run `uv export`"
    assert (REPO / "packages.txt").read_text(encoding="utf-8").split() == ["libgomp1"]
    assert (REPO / "streamlit_app.py").exists()


def test_a_cancellation_read_either_side_of_midnight_is_one_day(tmp_path):
    from datetime import timedelta

    from sitt.tz import IST

    db = tmp_path / "sitt.duckdb"
    with init_db(db) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
        night = datetime(2026, 10, 6, 23, 50, tzinfo=IST)
        rows = [
            (night - timedelta(hours=2), False, 3),  # a normal reading, so the train is listed
            (night, True, None),  # "cancelled" at 23:50 ...
            (night + timedelta(minutes=20), True, None),  # ... and again at 00:10
            (night + timedelta(days=2), True, None),  # two nights later: a second day
        ]
        for observed, cancelled, delay in rows:
            con.execute(
                "INSERT INTO observations (observed_at, train_id, station_code, delay_minutes, "
                "cancelled, source, event) VALUES (?, 'central-90111', 'KYN', ?, ?, 'ntes', ?)",
                [observed, delay, cancelled, "cancelled" if cancelled else "arrival"],
            )
    trains = data.delay_by_train(db)
    (days,) = trains.loc[trains["number"] == "90111", "cancelled_days"]
    assert days == 2


def test_cancellations_of_one_run_count_once_like_its_readings(tmp_path):
    """90103 starts from CSMT at 06:00ish: readings the evening before and that morning are
    about the same run, as `days` counts them; a fixed 03:00 cut would split them."""
    from datetime import timedelta

    from sitt.tz import IST

    db = tmp_path / "sitt.duckdb"
    with init_db(db) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
        morning = datetime(2026, 10, 7, 6, 0, tzinfo=IST)
        for observed, cancelled, delay in [
            (morning + timedelta(hours=1), False, 2),
            (morning - timedelta(hours=8), True, None),  # 22:00 the evening before
            (morning - timedelta(minutes=170), True, None),  # 03:10
            (morning - timedelta(minutes=190), True, None),  # 02:50
        ]:
            con.execute(
                "INSERT INTO observations (observed_at, train_id, station_code, delay_minutes, "
                "cancelled, source, event) VALUES (?, 'central-90103', 'KYN', ?, ?, 'ntes', ?)",
                [observed, delay, cancelled, "cancelled" if cancelled else "arrival"],
            )
    trains = data.delay_by_train(db)
    row = trains.loc[trains["number"] == "90103"].iloc[0]
    assert (row["days"], row["cancelled_days"]) == (1, 1)
