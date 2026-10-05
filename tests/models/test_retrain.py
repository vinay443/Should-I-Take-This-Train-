"""`sitt-retrain`: readiness gates, training, the registry and promotion.

The observations here are a TEST FIXTURE: invented readings, stored with source
'test-fixture', for the invented sample timetable. They exist to exercise both the
"gates not met" and the "gates met" paths. They are not real data and not the synthetic
generator's data either, and nothing is learned from them about real trains.
"""

import json
from datetime import UTC, date, datetime, timedelta

import duckdb
import numpy as np
import pytest
from conftest import SAMPLE_CSV

from sitt.config import RecommendSettings, RetrainSettings
from sitt.db import DatabaseBusyError, init_db
from sitt.ingest.timetable import load_timetable, read_timetable
from sitt.models import __main__ as models_cli
from sitt.models import retrain
from sitt.models.delay import DelayModel, ModelError
from sitt.recommend import LEVEL_BASELINE, LEVEL_MODEL, recommend
from sitt.tz import IST

FIXTURE_SOURCE = "test-fixture"
START = date(2026, 9, 1)
NOW = datetime(2026, 10, 12, 3, 30, tzinfo=UTC)
# Small gates, so a small fixture can meet them. The coverage band is widened because a
# quantile model fitted on a few hundred rows is rough; the real band is tested on its own
# in test_promotion_rule.
SETTINGS = RetrainSettings(
    min_days=14,
    min_observations=400,
    min_station_observations=150,
    min_test_observations=50,
    promote_coverage_min=0.5,
    promote_coverage_max=0.97,
)
# (train, station, minutes after midnight when it is due there)
POINTS = [
    ("90104", "KYN", 7 * 60 + 3), ("90104", "DI", 7 * 60 + 11), ("90104", "TNA", 7 * 60 + 31),
    ("90106", "KYN", 7 * 60 + 12), ("90106", "DI", 7 * 60 + 18), ("90106", "TNA", 7 * 60 + 33),
    ("90110", "KYN", 7 * 60 + 48), ("90110", "DI", 7 * 60 + 56),
]  # fmt: skip


def add_fixture_observations(con, days: int, seed: int = 1, stations=("KYN", "DI", "TNA")) -> int:
    """Invented readings: each train has its own habitual delay, plus a good or bad day."""
    rng = np.random.default_rng(seed)
    habit = {"90104": 2.0, "90106": 9.0, "90110": 5.0}
    rows = []
    for offset in range(days):
        day = START + timedelta(days=offset)
        bad_day = rng.normal(0, 1.5)
        for number, station, due in POINTS:
            if station not in stations:
                continue
            for lead in (20, 5):  # two polls before the train is due
                delay = round(max(0.0, habit[number] + bad_day + rng.normal(0, 0.7)))
                seen = datetime(day.year, day.month, day.day, tzinfo=IST) + timedelta(
                    minutes=due - lead
                )
                rows.append(
                    [seen, f"central-{number}", station, float(delay), FIXTURE_SOURCE, number,
                     "arrival", f"{seen.astimezone(UTC):%Y%m%dT%H%M%SZ}-fixtur"]
                )  # fmt: skip
    con.executemany(
        "INSERT INTO observations (observed_at, train_id, station_code, delay_minutes, source, "
        "train_number, event, batch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "data" / "sitt.duckdb"
    with init_db(path) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
    return path


@pytest.fixture
def folders(tmp_path):
    return tmp_path / "models", tmp_path / "models" / "delay"


def run(db, folders, settings=SETTINGS, **kwargs):
    lines: list[str] = []
    outcome = retrain.retrain(db, *folders, settings, now=NOW, log=lines.append, **kwargs)
    return outcome, lines


def registry(db) -> list[tuple]:
    with duckdb.connect(str(db), read_only=True) as con:
        return con.execute(
            "SELECT version, status, is_synthetic, rows_test, covered_stations FROM "
            "model_registry ORDER BY created_at"
        ).fetchall()


# --- gates not met ---


def test_with_too_little_data_it_reports_progress_and_trains_nothing(db, folders):
    with duckdb.connect(str(db)) as con:
        add_fixture_observations(con, days=3)
    outcome, lines = run(db, folders)
    assert not outcome.readiness.ready and outcome.manifest is None
    assert lines[0] == (
        "Not enough real data yet: days with observations: 3 of 14; usable observations: "
        "48 of 400; observations at the best-covered station: 18 of 150; observations in "
        "the last 7 days (the test window): 48 of 50."
    )
    assert lines[1:5] == [
        "  [ ] days with observations: 3 of 14",
        "  [ ] usable observations: 48 of 400",
        "  [ ] observations at the best-covered station: 18 of 150",
        "  [ ] observations in the last 7 days (the test window): 48 of 50",
    ]
    assert not folders[0].exists() and registry(db) == []


def test_an_empty_database_is_not_an_error(db, folders, monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert retrain.main(["--db", str(db), "--no-doc"], now=NOW) == 0
    assert "Not enough real data yet: days with observations: 0 of 28" in capsys.readouterr().out
    assert retrain.main(["--db", str(tmp_path / "nope.duckdb")]) == 2
    assert "No database at" in capsys.readouterr().err


def test_each_gate_is_checked_separately(db):
    with duckdb.connect(str(db)) as con:
        add_fixture_observations(con, days=30)
        ready = retrain.check_readiness(con, SETTINGS)
        assert ready.ready and ready.days == 30 and ready.observations == 480
        assert ready.by_station == {"KYN": 180, "DI": 180, "TNA": 120}
        assert ready.covered_stations == ["DI", "KYN"]  # Thane has too few of its own
        assert ready.by_source == {FIXTURE_SOURCE: 480}
        for change, name in (
            ({"min_days": 31}, "days with observations"),
            ({"min_observations": 481}, "usable observations"),
            ({"min_station_observations": 181}, "observations at the best-covered station"),
            ({"min_test_observations": 113}, "observations in the last 7 days (the test window)"),
        ):
            strict = retrain.check_readiness(con, RetrainSettings(**{**vars(SETTINGS), **change}))
            assert [gate.name for gate in strict.gates if not gate.met] == [name]


def test_flagged_readings_do_not_count_towards_the_gates(db, folders):
    with duckdb.connect(str(db)) as con:
        add_fixture_observations(con, days=3)
        # A reading no train could honestly have: 400 minutes late.
        con.execute(
            "INSERT INTO observations (observed_at, train_id, station_code, delay_minutes, "
            "source, train_number, event, batch_id) VALUES (?, 'central-90104', 'KYN', 400, ?, "
            "'90104', 'arrival', '20260905T010000Z-fixtur')",
            [datetime(2026, 9, 5, 6, 43, tzinfo=IST), FIXTURE_SOURCE],
        )
    outcome, _ = run(db, folders)
    assert outcome.readiness.observations == 48 and outcome.readiness.flagged == 1
    with duckdb.connect(str(db), read_only=True) as con:  # the reading itself is still there
        assert con.execute("SELECT count(*) FROM observations").fetchone() == (49,)


def test_synthetic_observations_are_refused(db, folders):
    with duckdb.connect(str(db)) as con:
        add_fixture_observations(con, days=30)
        con.execute("UPDATE observations SET source = 'synthetic' WHERE id % 50 = 0")
    with pytest.raises(ModelError, match="synthetic observations"):
        run(db, folders)
    assert registry(db) == []


# --- gates met ---


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    """One real training run on the fixture, shared by the tests below."""
    root = tmp_path_factory.mktemp("retrain")
    path = root / "data" / "sitt.duckdb"
    with init_db(path) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
        add_fixture_observations(con, days=40, seed=2)
    folders = (root / "models", root / "models" / "delay")
    doc = root / "model-results.md"
    doc.write_text("# Delay model results (SYNTHETIC RESULTS)\n\nSynthetic text.\n", "utf-8")
    lines: list[str] = []
    outcome = retrain.retrain(path, *folders, SETTINGS, now=NOW, log=lines.append, doc_path=doc)
    return path, folders, doc, outcome, lines


def test_a_ready_database_trains_a_versioned_model_with_a_manifest(trained):
    db, (models_dir, _live), _doc, outcome, _lines = trained
    assert outcome.readiness.ready and outcome.version_dir == models_dir / "real-20261012T033000Z"
    manifest = json.loads((outcome.version_dir / "manifest.json").read_text("utf-8"))
    assert manifest["version"] == "real-20261012T033000Z"
    assert manifest["is_synthetic"] is False and manifest["sources"] == [FIXTURE_SOURCE]
    assert manifest["data_window"]["first_day"] == "2026-09-01"
    assert manifest["data_window"]["last_day"] == "2026-10-10"
    # A time-based split: the test week is the last one, after validation, after training.
    periods = manifest["data_window"]["periods"]
    assert periods["train"][1] < periods["validation"][0] <= periods["validation"][1]
    assert periods["validation"][1] < periods["test"][0] and periods["test"][1] == "2026-10-10"
    assert manifest["row_counts"]["observations"] == 640
    assert manifest["row_counts"]["days"] == 40
    assert manifest["row_counts"]["feature_rows"]["test"] > 0
    assert "hist_delay" in manifest["features"] and "train_id" in manifest["features"]
    assert set(manifest["baseline_metrics"]) == {"zero", "train_station", "hour_weekday"}
    model = manifest["metrics"]["model"]
    assert {"mae", "rows", "range_coverage", "pinball_q10", "pinball_q90"} <= set(model)
    assert manifest["metrics"]["by_station"]["KYN"]["test_rows"] > 0
    assert manifest["covered_stations"] == ["DI", "KYN", "TNA"]
    assert all(gate["met"] for gate in manifest["gates"])
    assert manifest["git_commit"] is None or len(manifest["git_commit"]) == 40
    # The boosters are saved beside it and load back.
    saved = DelayModel.load(outcome.version_dir)
    assert saved.metadata["covered_stations"] == ["DI", "KYN", "TNA"]


def test_a_model_that_beats_the_baselines_is_promoted(trained):
    db, (_models, live), _doc, outcome, lines = trained
    decision = outcome.decision
    assert decision.promote, decision.reason
    assert decision.model_mae < decision.best_baseline_mae
    assert SETTINGS.promote_coverage_min <= decision.range_coverage <= 0.97
    # The baselines are real competition here, not a fallback to "always on time".
    baselines = outcome.manifest["baseline_metrics"]
    assert decision.best_baseline == "train_station"
    assert baselines["train_station"]["mae"] < baselines["zero"]["mae"] / 2
    assert registry(db) == [
        (
            "real-20261012T033000Z",
            "promoted",
            False,
            outcome.manifest["row_counts"]["feature_rows"]["test"],
            "DI,KYN,TNA",
        )
    ]
    live_model = DelayModel.load(live)
    assert not live_model.synthetic and live_model.metadata["version"] == "real-20261012T033000Z"
    assert any(line.startswith("Promoted real-20261012T033000Z: test MAE") for line in lines)


def test_the_docs_page_gets_a_marked_real_data_section(trained):
    _db, _folders, doc, outcome, _lines = trained
    text = doc.read_text("utf-8")
    assert text.startswith("# Delay model results (SYNTHETIC RESULTS)\n\nSynthetic text.\n")
    section = retrain.extract_real_section(text)
    assert section.startswith(retrain.BEGIN_MARK) and section.endswith(retrain.END_MARK)
    assert "## Real data so far" in section
    assert "640 usable observations on 40 day(s), 2026-09-01 to 2026-10-10" in section
    assert "**Enough real data to train on.**" in section
    assert "| Days with observations | 40 | 14 | yes |" in section
    assert "Latest: `real-20261012T033000Z`, **promoted**" in section
    test_rows = outcome.manifest["row_counts"]["feature_rows"]["test"]
    assert f"**Caveat: {test_rows:,} test rows is a small sample.**" in section
    assert "| LightGBM model |" in section and "| Baseline: always on time |" in section
    assert "| `KYN` |" in section and "| `real-20261012T033000Z` | promoted |" in section


def test_a_later_model_that_does_not_beat_the_baselines_is_not_promoted(trained):
    db, folders, doc, _first, _ = trained
    live_before = (folders[1] / "metadata.json").read_text("utf-8")
    # The same data, but a rule no model can meet: it must win by 100 minutes.
    strict = RetrainSettings(**{**vars(SETTINGS), "promote_min_mae_gain": 100.0})
    lines: list[str] = []
    later = NOW + timedelta(days=7)
    outcome = retrain.retrain(db, *folders, strict, now=later, log=lines.append, doc_path=doc)

    assert not outcome.decision.promote
    assert "does not beat the best baseline" in outcome.decision.reason
    assert [(row[0], row[1]) for row in registry(db)] == [
        ("real-20261012T033000Z", "promoted"),  # still the one in use
        ("real-20261019T033000Z", "rejected"),
    ]
    assert (folders[1] / "metadata.json").read_text("utf-8") == live_before
    assert (outcome.version_dir / "manifest.json").is_file()  # kept, for inspection
    assert "The model in use, if any, is unchanged." in lines
    section = retrain.extract_real_section(doc.read_text("utf-8"))
    assert "Latest: `real-20261019T033000Z`, **not promoted**" in section
    assert section.count(retrain.BEGIN_MARK) == 1  # replaced in place, not appended again

    # A third, promotable run supersedes the first.
    third = retrain.retrain(db, *folders, SETTINGS, now=later + timedelta(days=7), log=lines.append)
    assert third.decision.promote
    assert [row[1] for row in registry(db)] == ["superseded", "rejected", "promoted"]


def test_check_only_never_trains(trained, tmp_path):
    db, _folders, _doc, _outcome, _ = trained
    elsewhere = (tmp_path / "models", tmp_path / "models" / "delay")
    before = registry(db)
    outcome, lines = run(db, elsewhere, check_only=True)
    assert outcome.readiness.ready and outcome.manifest is None
    assert lines[0] == "Enough real data to train on."
    assert not elsewhere[0].exists() and registry(db) == before


# --- the promotion rule, on its own ---


def metadata(model_mae, zero, train_station, hour_weekday, coverage, synthetic=False) -> dict:
    return {
        "synthetic": synthetic,
        "metrics": {
            "all": {
                "model": {"mae": model_mae, "range_coverage": coverage},
                "zero": {"mae": zero},
                "train_station": {"mae": train_station},
                "hour_weekday": {"mae": hour_weekday},
            }
        },
    }


def test_promotion_rule():
    rules = RetrainSettings()
    good = retrain.decide(metadata(1.4, 4.0, 1.8, 2.7, 0.78), rules)
    assert good.promote and (good.best_baseline, good.best_baseline_mae) == ("train_station", 1.8)
    assert good.reason == (
        "test MAE 1.40 min beats the best baseline (median by train and station, 1.80 min) "
        "and its range holds 78% of test rows"
    )
    # The best baseline is whichever is best, not a fixed one.
    assert not retrain.decide(metadata(1.4, 1.3, 1.8, 2.7, 0.78), rules).promote
    assert not retrain.decide(metadata(1.8, 4.0, 1.8, 2.7, 0.78), rules).promote  # a tie
    narrow = retrain.decide(metadata(1.4, 4.0, 1.8, 2.7, 0.55), rules)
    assert not narrow.promote and "holds 55% of test rows, outside 70-90%" in narrow.reason
    assert not retrain.decide(metadata(1.4, 4.0, 1.8, 2.7, 0.97), rules).promote  # too wide
    margin = RetrainSettings(promote_min_mae_gain=0.5)
    assert not retrain.decide(metadata(1.4, 4.0, 1.8, 2.7, 0.78), margin).promote
    assert retrain.decide(metadata(1.2, 4.0, 1.8, 2.7, 0.78), margin).promote
    synthetic = retrain.decide(metadata(1.0, 4.0, 1.8, 2.7, 0.8, synthetic=True), rules)
    assert not synthetic.promote and "synthetic" in synthetic.reason


def test_promoting_moves_a_synthetic_model_aside_rather_than_losing_it(tmp_path):
    live, version = tmp_path / "models" / "delay", tmp_path / "models" / "real-x"
    for folder, content in ((live, {"synthetic": True}), (version, {"synthetic": False})):
        folder.mkdir(parents=True)
        (folder / "metadata.json").write_text(json.dumps(content), "utf-8")
    retrain.promote_files(version, live)
    assert json.loads((live / "metadata.json").read_text("utf-8")) == {"synthetic": False}
    aside = tmp_path / "models" / "delay-synthetic"
    assert json.loads((aside / "metadata.json").read_text("utf-8")) == {"synthetic": True}


# --- the docs page ---


def test_the_real_section_before_any_data(tmp_path):
    with init_db(":memory:") as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
        readiness = retrain.check_readiness(con)
    section = retrain.render_real_section(readiness, [], None, NOW)
    assert "No usable real observations yet." in section and "None trained yet." in section
    assert "**Not enough real data yet: days with observations: 0 of 28;" in section
    assert "It does not report\nCSMT-Kalyan locals" in section

    page = tmp_path / "page.md"
    assert retrain.update_doc(tmp_path / "missing.md", section) is False
    page.write_text("# Synthetic page\n", "utf-8")
    assert retrain.update_doc(page, section) and retrain.update_doc(page, section)
    assert page.read_text("utf-8").count(retrain.BEGIN_MARK) == 1
    assert page.read_text("utf-8").startswith("# Synthetic page\n\n" + retrain.BEGIN_MARK)


def test_rewriting_the_synthetic_report_keeps_the_real_section(trained, tmp_path):
    _db, (_models, live), doc, _outcome, _ = trained
    page = tmp_path / "model-results.md"
    page.write_text(doc.read_text("utf-8"), "utf-8")
    real = retrain.extract_real_section(page.read_text("utf-8"))
    assert models_cli.main(["report", "--model", str(live), "--write", str(page)]) == 0
    text = page.read_text("utf-8")
    assert text.startswith("# Delay model results\n")  # regenerated from the model
    assert retrain.extract_real_section(text) == real and text.endswith(retrain.END_MARK + "\n")


# --- a busy database ---


def test_a_locked_database_is_a_message_not_a_crash(db, folders, monkeypatch, capsys, tmp_path):
    def locked(*args, **kwargs):
        raise PermissionError("the file is in use")

    monkeypatch.setattr(retrain.shutil, "copy2", locked)
    monkeypatch.setattr(retrain.time, "sleep", lambda seconds: None)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(DatabaseBusyError):
        retrain.snapshot(db, tmp_path / "copy.duckdb")
    assert retrain.main(["--db", str(db), "--no-doc"]) == 2
    assert "in use by another process and could not be copied" in capsys.readouterr().err


# --- the recommender only uses the model where it was trained ---


def test_the_recommender_uses_a_real_model_only_at_covered_stations(trained):
    db, (_models, live), _doc, _outcome, _ = trained
    model = DelayModel.load(live)
    monday = datetime(2026, 10, 12, 6, 50)
    settings = RecommendSettings()
    with duckdb.connect(str(db)) as con:
        # Kalyan and Dombivli are covered: the model is used, with nothing to explain.
        inside = recommend(con, "KYN", "DI", monday, settings, model=model)
        assert inside.level == LEVEL_MODEL and not inside.synthetic
        assert not any("covers" in note for note in inside.notes)

        # Kalyan is covered, CSMT is not: the model at Kalyan, past delays (none here) at CSMT.
        mixed = recommend(con, "KYN", "CSMT", monday, settings, model=model)
        assert mixed.level == LEVEL_MODEL
        assert (
            "The delay model covers Kalyan only. Times at Chhatrapati Shivaji Maharaj Terminus "
            "use the typical past delay." in mixed.notes
        )
        assert all(option.arrival_delay == 0 for option in mixed.options)  # no CSMT history

        # Neither station is covered: the model is not used at all, and the reply says so.
        outside = recommend(con, "DR", "CSMT", monday, settings, model=model)
        assert outside.level == LEVEL_BASELINE
        assert any("was not trained on either of these stations" in n for n in outside.notes)

        # A model with no coverage list (an older or synthetic one) behaves as before.
        del model.metadata["covered_stations"]
        legacy = recommend(con, "DR", "CSMT", monday, settings, model=model)
        assert legacy.level == LEVEL_MODEL


def test_each_training_run_judges_the_long_distance_experiment(trained):
    _db, _folders, doc, outcome, _ = trained
    experiment = outcome.manifest["experiments"]["long_distance_feature"]
    assert experiment["features"] == ["ld_median_delay", "ld_count"]
    assert experiment["used_by_this_model"] is False  # the flag is off by default
    assert "ld_median_delay" not in outcome.manifest["features"]
    for side in ("with_feature", "without_feature"):
        assert set(experiment[side]) == {"mae", "within_2", "within_5", "range_coverage"}
    # The saved model is the one without the feature, so its figures are the "without" ones.
    assert experiment["without_feature"]["mae"] == outcome.manifest["metrics"]["model"]["mae"]
    # This fixture has no long-distance trains, and the page says how many rows had a value.
    assert experiment["feature_rows_with_a_value"] == 0 and experiment["feature_rows"] > 0
    section = retrain.extract_real_section(doc.read_text("utf-8"))
    assert "**Experiment: long-distance trains as a congestion signal.**" in section
    assert "The feature had a value in 0 of" in section
    assert "is noise, not a finding." in section
