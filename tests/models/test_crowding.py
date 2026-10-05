from dataclasses import replace
from datetime import UTC, datetime

import pytest

from sitt.bot import storage
from sitt.bot.parsing import CROWD_LEVELS
from sitt.db import init_db
from sitt.models.crowding import (
    CROWD_LABELS,
    RULES,
    CrowdingInput,
    CrowdingRules,
    estimate,
    rule_score,
    similar_reports,
)

MONDAY = datetime(2026, 10, 5)


def train(hhmm="14:00", direction="up", train_type="slow", **kwargs) -> CrowdingInput:
    hours, minutes = map(int, hhmm.split(":"))
    return CrowdingInput(
        direction, train_type, MONDAY.replace(hour=hours, minute=minutes), **kwargs
    )


def test_labels_match_the_bots_scale():
    assert CROWD_LABELS == CROWD_LEVELS


@pytest.mark.parametrize(
    ("inp", "score", "reason"),
    [
        (train("14:00"), 2, "off-peak"),
        (train("09:00"), 4, "peak hour towards CSMT"),
        (
            train("09:00", train_type="fast"),
            4,
            "peak hour towards CSMT, fast train",
        ),  # 4.5: halves round down
        (train("07:30"), 3, "edge of the peak towards CSMT"),
        (train("11:30"), 3, "edge of the peak towards CSMT"),
        (train("18:30"), 2, "against the peak flow"),  # 2.5: halves round down
        (train("18:30", direction="down"), 4, "peak hour away from CSMT"),
        (train("09:00", direction="down"), 2, "against the peak flow"),
        (train("23:30"), 1, "late night"),  # 1.5: halves round down
        (train("02:00", runs_ac=True), 1, "late night, AC"),
    ],
)
def test_time_and_direction(inp, score, reason):
    result = estimate(inp)
    assert (result.score, result.reason) == (score, reason)
    assert result.label == CROWD_LABELS[score]


def test_each_proxy_moves_the_score_the_documented_way():
    peak, reasons = rule_score(train("09:00"))
    assert (peak, reasons) == (4.0, ["peak hour towards CSMT"])

    def change(**kwargs):
        return rule_score(train("09:00", **kwargs))[0] - peak

    assert change(car_count=15) == RULES.fifteen_car == -0.5
    assert change(car_count=12) == 0
    assert change(runs_ac=True) == RULES.ac == -1.0
    assert change(runs_ac=False) == 0
    assert change(starts_here=True) == RULES.starts_here == -1.0
    assert change(is_ladies_special=True) == RULES.ladies_special == -0.5
    assert change(ahead_cancelled=True) == RULES.ahead_cancelled == 1.0
    assert change(ahead_delay=12) == RULES.ahead_late == 0.5
    assert change(ahead_delay=9) == 0  # not late enough to matter
    # A cancellation ahead already includes its passengers; lateness isn't added on top.
    assert change(ahead_cancelled=True, ahead_delay=30) == 1.0


def test_reasons_are_listed_and_the_score_stays_in_range():
    best = estimate(
        train("02:00", starts_here=True, runs_ac=True, car_count=15, is_ladies_special=True)
    )
    assert (best.score, best.rule_score) == (1, 1.0)
    assert best.reason == "late night, starts here, 15 cars, AC, ladies' special"

    worst = estimate(train("09:00", train_type="fast", ahead_cancelled=True))
    assert (worst.score, worst.label, worst.rule_score) == (5, "can't board", 5.0)
    assert worst.reason == "peak hour towards CSMT, fast train, the train before it is cancelled"

    late = estimate(train("14:00", ahead_delay=14.6))
    assert late.reason == "off-peak, the train before it is 15 min late"


def test_sunday_timetable_softens_the_peak():
    weekday = estimate(train("09:00"))
    sunday = estimate(train("09:00", sunday_schedule=True))
    assert (weekday.score, sunday.score) == (4, 3)  # 2 + 2.0 vs 2 + 0.8
    assert sunday.reason == "peak hour towards CSMT (Sunday timetable)"


def test_rules_are_tunable_in_one_place():
    gentler = replace(RULES, peak=1.0)
    assert estimate(train("09:00"), rules=gentler).score == 3
    assert CrowdingRules() == RULES


def test_reports_take_over_as_they_accumulate():
    peak = train("09:00")  # the rules say 4
    assert estimate(peak).reports_used == 0

    one = estimate(peak, [1])
    assert one.score == 3  # 0.75 * 4 + 0.25 * 1 = 3.25
    assert one.reason == "peak hour towards CSMT, your 1 report average 1.0"
    assert (one.rule_score, one.reports_mean) == (4.0, 1.0)

    three = estimate(peak, [1, 1, 1])
    assert three.score == 2  # halfway: 2.5, and halves round down
    nine = estimate(peak, [1] * 9)
    assert nine.score == 2  # 0.25 * 4 + 0.75 * 1 = 1.75
    many = estimate(peak, [1] * 60)
    assert many.score == 1
    assert many.reason.endswith("your 60 reports average 1.0")

    # Reports that agree with the rules change nothing.
    assert estimate(peak, [4, 4]).score == 4
    # A mix uses the mean.
    assert estimate(train("14:00"), [5, 5, 4]).reports_mean == pytest.approx(14 / 3)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.duckdb"
    init_db(path).close()
    return path


def _log(db, description, level, station="KYN", when=datetime(2026, 10, 1, 4, 0, tzinfo=UTC)):
    storage.insert_report(
        db,
        storage.NewReport(
            telegram_user_id=1,
            reported_at=when,
            station_code=station,
            train_description=description,
            crowd_level=level,
            note=None,
        ),
    )


def test_similar_reports(db):
    _log(db, "08:12 fast from KYN", 5)
    _log(db, "08:40 fast from KYN", 4)  # 28 minutes off: counts
    _log(db, "08:45 fast from KYN", 1)  # 33 minutes off: too far
    _log(db, "08:12 slow from KYN", 2)  # other type
    _log(db, "08:12 fast from TNA", 1, station="TNA")  # other station
    _log(db, "08:10 fast from KYN", 1, when=datetime(2026, 5, 1, tzinfo=UTC))  # too old
    departure = datetime(2026, 10, 5, 8, 12)
    now = datetime(2026, 10, 5, 8, 0)

    with init_db(db) as con:
        assert sorted(similar_reports(con, "KYN", "fast", departure, now)) == [4, 5]
        assert similar_reports(con, "KYN", "slow", departure, now) == [2]
        assert similar_reports(con, "DR", "fast", departure, now) == []
        # The window wraps midnight.
        _log_direct = "INSERT INTO crowd_reports (reported_at, train_description, station_code,"
        con.execute(
            _log_direct
            + " crowd_level, source) VALUES (now(), '23:55 slow from KYN', 'KYN', 3, 't')"
        )
        late = datetime(2026, 10, 6, 0, 10)
        assert similar_reports(con, "KYN", "slow", late, datetime(2026, 10, 6, 0, 0)) == [3]
        # A wider window, set in the rules, takes in the 08:45 report too.
        wide = replace(RULES, report_window_minutes=40)
        assert len(similar_reports(con, "KYN", "fast", departure, now, wide)) == 3
