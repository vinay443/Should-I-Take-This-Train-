"""Settings read from environment variables, plus a `.env` file in the working directory."""

import os
import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

DEFAULT_DB_PATH = "data/sitt.duckdb"
DEFAULT_MODEL_DIR = "models/delay"


@dataclass(frozen=True)
class RecommendSettings:
    """Thresholds for "take this train or wait?" (see sitt.recommend).

    Each can be set with the environment variable named beside it.
    """

    candidates: int = 5  # SITT_RECOMMEND_CANDIDATES: how many upcoming trains to compare
    # SITT_WAIT_MAX_EXTRA_MINUTES: a later, emptier train is worth waiting for only if it
    # arrives no more than this long after the earliest arrival.
    wait_max_extra_minutes: float = 5.0
    # SITT_CROWD_GAIN_LEVELS: and only if it is at least this many crowding levels emptier.
    crowd_gain_levels: int = 1
    # SITT_VERY_LATE_MINUTES: a train predicted this late at the boarding station is
    # "very late": too unreliable to recommend if another arrives soon after.
    very_late_minutes: float = 15.0
    # SITT_VERY_LATE_SLACK_MINUTES: "soon after" for the rule above.
    very_late_slack_minutes: float = 10.0
    # SITT_LADIES_SPECIAL_OK: set to true if the rider can board ladies' specials.
    # Otherwise they are listed but never recommended.
    ladies_special_ok: bool = False
    # SITT_ALLOW_SYNTHETIC_MODEL: use a delay model even if it was trained on synthetic
    # (invented) data. For testing only. Off by default, so such a model is ignored and
    # predictions fall back to past observations or the timetable.
    allow_synthetic_model: bool = False


@dataclass(frozen=True)
class BlockSettings:
    """How megablocks are interpreted (see sitt.models.features and docs/megablocks.md).

    A block announced without times is assumed to run between these hours, Mumbai time,
    which is when Sunday megablocks usually are. Set with SITT_BLOCK_DEFAULT_START and
    SITT_BLOCK_DEFAULT_END, as HH:MM.
    """

    default_start: time = time(10, 0)
    default_end: time = time(16, 0)


@dataclass(frozen=True)
class HealthSettings:
    """What `sitt-health` expects of the collector, and when it raises an alert.

    Each can be set with the environment variable named beside it. See docs/collector.md.
    """

    # SITT_COLLECT_CADENCE_MINUTES: how often the collector is scheduled to run.
    cadence_minutes: float = 15.0
    # SITT_HEALTH_GAP_TOLERANCE_MINUTES: two runs further apart than the cadence plus this
    # have a gap between them. Task Scheduler starts runs a little late, never early.
    gap_tolerance_minutes: float = 10.0
    # SITT_ALERT_NO_RUN_HOURS: alert when no run has succeeded for this long.
    alert_no_run_hours: float = 3.0
    # SITT_ALERT_SOURCE_DOWN_RUNS: alert when a source failed in this many runs in a row.
    alert_source_down_runs: int = 4
    # SITT_ALERT_MIN_READINGS: alert when a source's mean readings per run, over its last
    # SITT_ALERT_READINGS_RUNS successful runs, is below this.
    alert_min_readings: float = 10.0
    alert_readings_runs: int = 4
    # SITT_ALERT_REPEAT_HOURS: repeat an alert that is still true no more often than this.
    alert_repeat_hours: float = 12.0
    # SITT_TELEGRAM_SEND: really send Telegram messages. Off by default, so that
    # `sitt-health --telegram` prints what it would send (a dry run) until this is true.
    telegram_send: bool = False


@dataclass(frozen=True)
class DQSettings:
    """Bounds for the data-quality checks on observations (see sitt.dq, docs/data-quality.md).

    Each can be set with the environment variable named beside it. All are in minutes.
    "Suburban" means a train matched to the timetable; anything else is treated as a
    long-distance train, which can honestly run hours late.
    """

    # SITT_DQ_NEAR_DUPLICATE_MINUTES: the same train, station and event read again within
    # this long by a different batch is a near duplicate (two collectors running at once).
    near_duplicate_minutes: float = 5.0
    # SITT_DQ_MAX_EARLY_MINUTES / SITT_DQ_MAX_DELAY_MINUTES: suburban trains outside
    # [-early, +delay] are implausible.
    max_early_minutes: float = 15.0
    max_delay_minutes: float = 180.0
    # SITT_DQ_MAX_EARLY_MINUTES_LONG_DISTANCE / SITT_DQ_MAX_DELAY_MINUTES_LONG_DISTANCE
    max_early_minutes_long_distance: float = 120.0
    max_delay_minutes_long_distance: float = 1440.0
    # SITT_DQ_JUMP_WINDOW_MINUTES / SITT_DQ_JUMP_SLACK_MINUTES: between two readings of the
    # same train at the same station no more than the window apart, the delay can't
    # honestly change by more than the time that passed plus the slack.
    jump_window_minutes: float = 60.0
    jump_slack_minutes: float = 15.0
    # SITT_DQ_SCHEDULE_MISMATCH_MINUTES: the source's delay and the delay implied by our
    # timetable disagree by more than this.
    schedule_mismatch_minutes: float = 20.0
    # SITT_DQ_FAR_FROM_SCHEDULE_MINUTES: a reading taken further than this from when the
    # train was due at the station (allowing for its delay): possibly matched to the wrong day.
    far_from_schedule_minutes: float = 240.0
    # SITT_DQ_FUTURE_TOLERANCE_MINUTES: a reading stamped later than "now" by more than this.
    future_tolerance_minutes: float = 5.0
    # SITT_DQ_BATCH_TIME_TOLERANCE_MINUTES: a reading stamped this far from its batch's
    # own time. About 330 minutes points at a UTC/IST mix-up.
    batch_time_tolerance_minutes: float = 90.0


@dataclass(frozen=True)
class MatchSettings:
    """How a crowd report is matched to a scheduled train (see sitt.matching, docs/crowding.md).

    Each can be set with the environment variable named beside it.
    """

    # SITT_MATCH_WINDOW_MINUTES: a train this far or further from the reported time scores
    # nothing. Closer trains score more, up to 1 for the exact minute.
    window_minutes: float = 10.0
    # SITT_MATCH_MIN_CONFIDENCE: below this, the report is left unmatched.
    min_confidence: float = 0.6
    # SITT_MATCH_FUTURE_MINUTES: a report may be about a train leaving up to this long
    # after it was logged (logging from the platform); otherwise the time is in the past.
    future_minutes: float = 60.0
    # SITT_MATCH_CHOICE_WINDOW_MINUTES: trains within this of the reported time are
    # offered as buttons when the rider wants to pick the train.
    choice_window_minutes: float = 20.0
    # SITT_MATCH_CHOICES: how many trains to offer.
    choices: int = 4


@dataclass(frozen=True)
class CommuteSettings:
    """`/commute`, the morning message and the after-commute crowd prompt (docs/bot-setup.md).

    Each can be set with the environment variable named beside it.
    """

    # SITT_COMMUTE_RETURN_AFTER: when two favourites are each other's reverse, /commute
    # gives the outbound one before this time of day and the return one from then on.
    return_after: time = time(14, 0)
    # SITT_COMMUTE_NOTIFY: send the recommendation before the usual train. On by default,
    # but nothing is sent for a favourite without a usual departure time.
    notify_enabled: bool = True
    # SITT_COMMUTE_NOTIFY_LEAD_MINUTES: how long before the usual train to send it.
    notify_lead_minutes: float = 20.0
    # SITT_COMMUTE_NUDGE: ask how crowded the usual train was once it has arrived.
    nudge_enabled: bool = True
    # SITT_COMMUTE_NUDGE_AFTER_MINUTES: how long after its scheduled arrival to ask.
    nudge_after_minutes: float = 5.0
    # SITT_COMMUTE_SKIP_SUNDAY_SCHEDULE: send neither on Sundays and holidays.
    skip_sunday_schedule: bool = True
    # SITT_COMMUTE_GRACE_MINUTES: a message whose moment passed while the bot was busy or
    # restarting is still sent if no more than this late.
    grace_minutes: float = 10.0
    # SITT_COMMUTE_USUAL_TRAIN_MINUTES: the "usual train" is the scheduled train leaving
    # within this many minutes of the favourite's usual time.
    usual_train_minutes: float = 10.0


@dataclass(frozen=True)
class RetrainSettings:
    """When there is enough real data to train on, and when a model is good enough to use.

    See sitt.models.retrain and docs/model-results.md. Each can be set with the
    environment variable named beside it. All are guesses to revisit with real data.
    """

    # --- readiness gates: all must hold before anything is trained ---
    # SITT_RETRAIN_MIN_DAYS: distinct service days with usable observations.
    min_days: int = 28
    # SITT_RETRAIN_MIN_OBSERVATIONS: usable observations in total.
    min_observations: int = 5000
    # SITT_RETRAIN_MIN_STATION_OBSERVATIONS: a station needs this many to count as
    # covered, and at least one station must be covered.
    min_station_observations: int = 300
    # SITT_RETRAIN_MIN_TEST_OBSERVATIONS: usable observations in the held-out test weeks.
    min_test_observations: int = 500
    # SITT_RETRAIN_TEST_WEEKS / SITT_RETRAIN_VALID_WEEKS: the time-based split.
    test_weeks: int = 1
    valid_weeks: int = 1

    # --- promotion: a trained model replaces the one in use only if ---
    # SITT_PROMOTE_MIN_MAE_GAIN: its test MAE is lower than the best baseline's by more
    # than this many minutes, and
    promote_min_mae_gain: float = 0.0
    # SITT_PROMOTE_COVERAGE_MIN / SITT_PROMOTE_COVERAGE_MAX: its 10th-90th percentile
    # range holds this share of test rows (80% would be perfect).
    promote_coverage_min: float = 0.70
    promote_coverage_max: float = 0.90


@dataclass(frozen=True)
class Settings:
    db_path: Path
    log_level: str
    telegram_bot_token: str | None
    allowed_user_ids: frozenset[int]
    model_dir: Path = Path(DEFAULT_MODEL_DIR)
    recommend: RecommendSettings = field(default_factory=RecommendSettings)
    match: MatchSettings = field(default_factory=MatchSettings)
    commute: CommuteSettings = field(default_factory=CommuteSettings)


def parse_allowed_user_ids(raw: str | None) -> frozenset[int]:
    """Parse Telegram user IDs separated by commas and/or whitespace, e.g. "123, 456"."""
    parts = [part for part in re.split(r"[,\s]+", raw or "") if part]
    for part in parts:
        if not re.fullmatch(r"\d+", part, re.ASCII):
            raise ValueError(f"ALLOWED_USER_IDS contains {part!r}, which is not a Telegram user ID")
    return frozenset(int(part) for part in parts)


def _number(name: str, default: float, kind: type = float):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = kind(raw)
    except ValueError:
        raise ValueError(f"{name} is {raw!r}, which is not a number") from None
    if value < 0:
        raise ValueError(f"{name} is {raw!r}; it can't be negative")
    return value


def _flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes")


def _switch(name: str, default: bool) -> bool:
    """A flag with a default: unset keeps the default, anything else is read as yes or no."""
    raw = (os.environ.get(name) or "").strip().lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


def load_recommend_settings() -> RecommendSettings:
    defaults = RecommendSettings()
    return RecommendSettings(
        candidates=max(1, _number("SITT_RECOMMEND_CANDIDATES", defaults.candidates, int)),
        wait_max_extra_minutes=_number(
            "SITT_WAIT_MAX_EXTRA_MINUTES", defaults.wait_max_extra_minutes
        ),
        crowd_gain_levels=_number("SITT_CROWD_GAIN_LEVELS", defaults.crowd_gain_levels, int),
        very_late_minutes=_number("SITT_VERY_LATE_MINUTES", defaults.very_late_minutes),
        very_late_slack_minutes=_number(
            "SITT_VERY_LATE_SLACK_MINUTES", defaults.very_late_slack_minutes
        ),
        ladies_special_ok=_flag("SITT_LADIES_SPECIAL_OK"),
        allow_synthetic_model=_flag("SITT_ALLOW_SYNTHETIC_MODEL"),
    )


def _clock(name: str, default: time) -> time:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return time.fromisoformat(raw if len(raw) > 4 else f"0{raw}")
    except ValueError:
        raise ValueError(f"{name} is {raw!r}, which is not a time like 10:00") from None


def load_block_settings() -> BlockSettings:
    defaults = BlockSettings()
    return BlockSettings(
        default_start=_clock("SITT_BLOCK_DEFAULT_START", defaults.default_start),
        default_end=_clock("SITT_BLOCK_DEFAULT_END", defaults.default_end),
    )


def load_health_settings() -> HealthSettings:
    defaults = HealthSettings()
    return HealthSettings(
        cadence_minutes=max(1.0, _number("SITT_COLLECT_CADENCE_MINUTES", defaults.cadence_minutes)),
        gap_tolerance_minutes=_number(
            "SITT_HEALTH_GAP_TOLERANCE_MINUTES", defaults.gap_tolerance_minutes
        ),
        alert_no_run_hours=_number("SITT_ALERT_NO_RUN_HOURS", defaults.alert_no_run_hours),
        alert_source_down_runs=max(
            1, _number("SITT_ALERT_SOURCE_DOWN_RUNS", defaults.alert_source_down_runs, int)
        ),
        alert_min_readings=_number("SITT_ALERT_MIN_READINGS", defaults.alert_min_readings),
        alert_readings_runs=max(
            1, _number("SITT_ALERT_READINGS_RUNS", defaults.alert_readings_runs, int)
        ),
        alert_repeat_hours=_number("SITT_ALERT_REPEAT_HOURS", defaults.alert_repeat_hours),
        telegram_send=_flag("SITT_TELEGRAM_SEND"),
    )


def load_dq_settings() -> DQSettings:
    defaults = DQSettings()
    names = {
        "near_duplicate_minutes": "SITT_DQ_NEAR_DUPLICATE_MINUTES",
        "max_early_minutes": "SITT_DQ_MAX_EARLY_MINUTES",
        "max_delay_minutes": "SITT_DQ_MAX_DELAY_MINUTES",
        "max_early_minutes_long_distance": "SITT_DQ_MAX_EARLY_MINUTES_LONG_DISTANCE",
        "max_delay_minutes_long_distance": "SITT_DQ_MAX_DELAY_MINUTES_LONG_DISTANCE",
        "jump_window_minutes": "SITT_DQ_JUMP_WINDOW_MINUTES",
        "jump_slack_minutes": "SITT_DQ_JUMP_SLACK_MINUTES",
        "schedule_mismatch_minutes": "SITT_DQ_SCHEDULE_MISMATCH_MINUTES",
        "far_from_schedule_minutes": "SITT_DQ_FAR_FROM_SCHEDULE_MINUTES",
        "future_tolerance_minutes": "SITT_DQ_FUTURE_TOLERANCE_MINUTES",
        "batch_time_tolerance_minutes": "SITT_DQ_BATCH_TIME_TOLERANCE_MINUTES",
    }
    return DQSettings(
        **{field: _number(name, getattr(defaults, field)) for field, name in names.items()}
    )


def load_match_settings() -> MatchSettings:
    defaults = MatchSettings()
    return MatchSettings(
        window_minutes=max(1.0, _number("SITT_MATCH_WINDOW_MINUTES", defaults.window_minutes)),
        min_confidence=min(1.0, _number("SITT_MATCH_MIN_CONFIDENCE", defaults.min_confidence)),
        future_minutes=_number("SITT_MATCH_FUTURE_MINUTES", defaults.future_minutes),
        choice_window_minutes=_number(
            "SITT_MATCH_CHOICE_WINDOW_MINUTES", defaults.choice_window_minutes
        ),
        choices=max(1, _number("SITT_MATCH_CHOICES", defaults.choices, int)),
    )


def load_commute_settings() -> CommuteSettings:
    defaults = CommuteSettings()
    return CommuteSettings(
        return_after=_clock("SITT_COMMUTE_RETURN_AFTER", defaults.return_after),
        notify_enabled=_switch("SITT_COMMUTE_NOTIFY", defaults.notify_enabled),
        notify_lead_minutes=_number(
            "SITT_COMMUTE_NOTIFY_LEAD_MINUTES", defaults.notify_lead_minutes
        ),
        nudge_enabled=_switch("SITT_COMMUTE_NUDGE", defaults.nudge_enabled),
        nudge_after_minutes=_number(
            "SITT_COMMUTE_NUDGE_AFTER_MINUTES", defaults.nudge_after_minutes
        ),
        skip_sunday_schedule=_switch(
            "SITT_COMMUTE_SKIP_SUNDAY_SCHEDULE", defaults.skip_sunday_schedule
        ),
        grace_minutes=max(1.0, _number("SITT_COMMUTE_GRACE_MINUTES", defaults.grace_minutes)),
        usual_train_minutes=_number(
            "SITT_COMMUTE_USUAL_TRAIN_MINUTES", defaults.usual_train_minutes
        ),
    )


def load_retrain_settings() -> RetrainSettings:
    d = RetrainSettings()
    return RetrainSettings(
        min_days=_number("SITT_RETRAIN_MIN_DAYS", d.min_days, int),
        min_observations=_number("SITT_RETRAIN_MIN_OBSERVATIONS", d.min_observations, int),
        min_station_observations=_number(
            "SITT_RETRAIN_MIN_STATION_OBSERVATIONS", d.min_station_observations, int
        ),
        min_test_observations=_number(
            "SITT_RETRAIN_MIN_TEST_OBSERVATIONS", d.min_test_observations, int
        ),
        test_weeks=max(1, _number("SITT_RETRAIN_TEST_WEEKS", d.test_weeks, int)),
        valid_weeks=max(1, _number("SITT_RETRAIN_VALID_WEEKS", d.valid_weeks, int)),
        promote_min_mae_gain=_number("SITT_PROMOTE_MIN_MAE_GAIN", d.promote_min_mae_gain),
        promote_coverage_min=_number("SITT_PROMOTE_COVERAGE_MIN", d.promote_coverage_min),
        promote_coverage_max=_number("SITT_PROMOTE_COVERAGE_MAX", d.promote_coverage_max),
    )


def long_distance_feature_enabled() -> bool:
    """SITT_FEATURE_LONG_DISTANCE: train delay models with the long-distance congestion
    inputs (sitt.models.features.LONG_DISTANCE_FEATURES). An experiment; off by default."""
    return _flag("SITT_FEATURE_LONG_DISTANCE")


def load_settings() -> Settings:
    # Variables already set in the environment take precedence over `.env`.
    load_dotenv(find_dotenv(usecwd=True))
    return Settings(
        db_path=Path(os.environ.get("SITT_DB_PATH") or DEFAULT_DB_PATH),
        log_level=os.environ.get("SITT_LOG_LEVEL") or "INFO",
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        allowed_user_ids=parse_allowed_user_ids(os.environ.get("ALLOWED_USER_IDS")),
        model_dir=Path(os.environ.get("SITT_MODEL_DIR") or DEFAULT_MODEL_DIR),
        recommend=load_recommend_settings(),
        match=load_match_settings(),
        commute=load_commute_settings(),
    )
