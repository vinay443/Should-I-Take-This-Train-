"""A rule-of-thumb crowding score for a train at a boarding station.

Nobody publishes how full Mumbai locals are (docs/data-sources.md, section 3), so this
is an estimate from proxies, not a measurement. It gives a score from 1 (empty) to 5
(can't board), the same scale riders use when they log a trip with the bot, and a short
reason.

The rules
---------
Every number is in `CrowdingRules`, in one place, and each is a guess to be tuned.
Start from `base` and add:

- **Peak.** `peak` if the train leaves the boarding station in the peak for its
  direction (towards CSMT in the morning, away in the evening), half-weight `shoulder`
  in the hour either side, and `counter_peak` if it runs against the flow at those
  hours. On a Sunday-timetable day the peak terms are scaled by `sunday_peak_share`.
  `night` applies between 23:00 and 05:00.
- **Fast train in the peak:** `fast_in_peak`, because fast trains fill first.
- **15 cars:** `fifteen_car` (a quarter more room).
- **AC:** `ac` (dearer tickets, fewer riders). Only when that run is air-conditioned.
- **Starts here:** `starts_here`, because an empty train is easy to board.
- **Ladies' special:** `ladies_special` (fewer people may board).
- **Train ahead:** `ahead_cancelled` if the train before it was cancelled, or
  `ahead_late` if it is at least `ahead_late_minutes` late, since its passengers wait
  for this one.

The result is clamped to 1-5 and rounded to the nearest level. A score exactly halfway
(2.5, 4.5) rounds down, so a train needs more than half a step to move up a level.

Blending in the rider's own reports
-----------------------------------
`similar_reports` finds crowd reports logged with the bot for a similar train: the same
boarding station and fast/slow type, a departure time within `report_window_minutes`,
and logged in the last `report_max_age_days`. With `n` such reports of mean level `m`,
the estimate becomes

    (1 - w) * rule_score + w * m,   where w = n / (n + report_half_weight)

so one report moves the estimate a quarter of the way to what was seen, three reports
halfway, and nine reports three-quarters (with the default `report_half_weight` of 3).
The reports gradually take over from the rules as they accumulate.

Reports matched to this exact train
-----------------------------------
A report that `sitt.matching` has tied to a train (`crowd_reports.matched_train_id`)
says more about that train than a report about a neighbour does. `train_reports` finds
them. When a train has any, each counts as one report and each merely similar report
counts as `report_similar_weight` (half) of one, in both `n` and the mean. A train with
no matched reports is scored exactly as before, from similar reports alone.
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import duckdb

from sitt.models.features import local_naive

CROWD_LABELS: dict[int, str] = {
    1: "empty",
    2: "seats free",
    3: "standing",
    4: "packed",
    5: "can't board",
}


@dataclass(frozen=True)
class CrowdingRules:
    base: float = 2.0  # off-peak: seats free

    # Peak hours as (start, end) in hours at the boarding station.
    up_peak: tuple[float, float] = (8.0, 11.0)  # towards CSMT
    down_peak: tuple[float, float] = (17.5, 21.0)  # away from CSMT
    shoulder_hours: float = 1.0
    peak: float = 2.0
    shoulder: float = 1.0
    counter_peak: float = 0.5
    sunday_peak_share: float = 0.4
    night: float = -0.5
    night_hours: tuple[float, float] = (23.0, 5.0)

    fast_in_peak: float = 0.5
    fifteen_car: float = -0.5
    ac: float = -1.0
    starts_here: float = -1.0
    ladies_special: float = -0.5
    ahead_cancelled: float = 1.0
    ahead_late: float = 0.5
    ahead_late_minutes: float = 10.0

    # Blending in crowd reports.
    report_window_minutes: int = 30
    report_max_age_days: int = 90
    report_half_weight: float = 3.0
    # What a similar train's report is worth when this train has reports of its own.
    report_similar_weight: float = 0.5


RULES = CrowdingRules()


@dataclass(frozen=True)
class CrowdingInput:
    """What is known about one train at the station where the rider boards."""

    direction: str  # 'up' or 'down'
    train_type: str  # 'fast' or 'slow'
    departure: datetime  # scheduled departure from the boarding station
    starts_here: bool = False
    car_count: int | None = None
    runs_ac: bool | None = None
    is_ladies_special: bool | None = None
    sunday_schedule: bool = False
    ahead_cancelled: bool = False
    ahead_delay: float | None = None  # minutes late the train before it is running


@dataclass(frozen=True)
class CrowdingEstimate:
    score: int  # 1-5
    label: str
    reason: str
    rule_score: float  # before blending in reports, unrounded
    reports_used: int = 0
    reports_mean: float | None = None
    train_reports_used: int = 0  # how many of `reports_used` were for this exact train


def _in_window(hour: float, window: tuple[float, float]) -> bool:
    start, end = window
    return start <= hour <= end if start <= end else hour >= start or hour <= end


def _peak_terms(inp: CrowdingInput, rules: CrowdingRules) -> tuple[float, str | None, bool]:
    """(score change, reason, whether the train is in its own peak)."""
    hour = inp.departure.hour + inp.departure.minute / 60
    own, other = (
        (rules.up_peak, rules.down_peak)
        if inp.direction == "up"
        else (rules.down_peak, rules.up_peak)
    )
    toward = "towards CSMT" if inp.direction == "up" else "away from CSMT"
    scale = rules.sunday_peak_share if inp.sunday_schedule else 1.0
    quiet_day = " (Sunday timetable)" if inp.sunday_schedule else ""
    if _in_window(hour, own):
        return rules.peak * scale, f"peak hour {toward}{quiet_day}", True
    shoulder = (own[0] - rules.shoulder_hours, own[1] + rules.shoulder_hours)
    if _in_window(hour, shoulder):
        return rules.shoulder * scale, f"edge of the peak {toward}{quiet_day}", False
    if _in_window(hour, other):
        return rules.counter_peak * scale, "against the peak flow", False
    if _in_window(hour, rules.night_hours):
        return rules.night, "late night", False
    return 0.0, "off-peak", False


def rule_score(inp: CrowdingInput, rules: CrowdingRules = RULES) -> tuple[float, list[str]]:
    """The unrounded score from the rules alone, and the reasons behind it."""
    change, reason, in_peak = _peak_terms(inp, rules)
    score = rules.base + change
    reasons = [reason] if reason else []
    if in_peak and inp.train_type == "fast":
        score += rules.fast_in_peak
        reasons.append("fast train")
    if inp.ahead_cancelled:
        score += rules.ahead_cancelled
        reasons.append("the train before it is cancelled")
    elif inp.ahead_delay is not None and inp.ahead_delay >= rules.ahead_late_minutes:
        score += rules.ahead_late
        reasons.append(f"the train before it is {round(inp.ahead_delay)} min late")
    if inp.starts_here:
        score += rules.starts_here
        reasons.append("starts here")
    if inp.car_count == 15:
        score += rules.fifteen_car
        reasons.append("15 cars")
    if inp.runs_ac:
        score += rules.ac
        reasons.append("AC")
    if inp.is_ladies_special:
        score += rules.ladies_special
        reasons.append("ladies' special")
    return min(5.0, max(1.0, score)), reasons


def estimate(
    inp: CrowdingInput,
    reports: Sequence[int] = (),
    rules: CrowdingRules = RULES,
    train_reports: Sequence[int] = (),
) -> CrowdingEstimate:
    """Score one train, blending in crowd reports.

    `reports` are levels logged for similar trains; `train_reports` are levels logged
    for this very train, which count for more (see the module docstring).
    """
    from_rules, reasons = rule_score(inp, rules)
    blended = from_rules
    mean = None
    similar_weight = rules.report_similar_weight if train_reports else 1.0
    effective = len(train_reports) + similar_weight * len(reports)
    if effective > 0:
        mean = (sum(train_reports) + similar_weight * sum(reports)) / effective
        weight = effective / (effective + rules.report_half_weight)
        blended = (1 - weight) * from_rules + weight * mean
        if train_reports:
            plural = "s" if len(train_reports) != 1 else ""
            text = f"your {len(train_reports)} report{plural} for this train"
            if reports:
                text += f" and {len(reports)} for similar trains"
            reasons.append(f"{text} average {mean:.1f}")
        else:
            plural = "s" if len(reports) != 1 else ""
            reasons.append(f"your {len(reports)} report{plural} average {mean:.1f}")
    score = int(min(5, max(1, math.ceil(blended - 0.5))))
    return CrowdingEstimate(
        score=score,
        label=CROWD_LABELS[score],
        reason=", ".join(reasons),
        rule_score=from_rules,
        reports_used=len(reports) + len(train_reports),
        reports_mean=mean,
        train_reports_used=len(train_reports),
    )


_DESCRIPTION_RE = re.compile(r"(\d{1,2}):(\d{2}) (fast|slow) from (\S+)")


def similar_reports(
    con: duckdb.DuckDBPyConnection,
    station_code: str,
    train_type: str,
    departure: datetime,
    now: datetime | None = None,
    rules: CrowdingRules = RULES,
    exclude_train_id: str | None = None,
) -> list[int]:
    """Crowd levels the rider logged for trains like this one. See the module docstring.

    Reports matched to `exclude_train_id` are left out, so that a train's own reports
    (see `train_reports`) aren't counted twice.
    """
    now = local_naive(now) if now else datetime.now()
    oldest = now - timedelta(days=rules.report_max_age_days)
    own = (
        "AND matched_train_id IS DISTINCT FROM ?" if exclude_train_id and _has_matches(con) else ""
    )
    rows = con.execute(
        "SELECT crowd_level, train_description, epoch_ms(reported_at) FROM crowd_reports "
        f"WHERE station_code = ? AND train_description IS NOT NULL {own}",
        [station_code, exclude_train_id] if own else [station_code],
    ).fetchall()
    wanted = departure.hour * 60 + departure.minute
    levels = []
    for level, description, reported_ms in rows:
        match = _DESCRIPTION_RE.fullmatch(description.strip())
        if match is None or match[3] != train_type or match[4] != station_code:
            continue
        reported = datetime(1970, 1, 1) + timedelta(milliseconds=reported_ms + 19_800_000)
        if reported < oldest:
            continue
        gap = abs(int(match[1]) * 60 + int(match[2]) - wanted)
        if min(gap, 1440 - gap) <= rules.report_window_minutes:
            levels.append(int(level))
    return levels


def _has_matches(con: duckdb.DuckDBPyConnection) -> bool:
    """Whether `crowd_reports` has the matching columns. An old database may not yet."""
    return (
        con.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'crowd_reports' AND column_name = 'matched_train_id'"
        ).fetchone()[0]
        > 0
    )


def train_reports(
    con: duckdb.DuckDBPyConnection,
    train_id: str,
    station_code: str,
    now: datetime | None = None,
    rules: CrowdingRules = RULES,
) -> list[int]:
    """Crowd levels logged for this exact train, boarded at this station.

    These are the reports `sitt.matching` tied to the train, whichever way: at log time,
    by backfill, by the rider's own pick, or from the after-commute prompt.
    """
    if not _has_matches(con):
        return []
    now = local_naive(now) if now else datetime.now()
    oldest_ms = (
        now - timedelta(days=rules.report_max_age_days) - datetime(1970, 1, 1)
    ).total_seconds() * 1000 - 19_800_000
    rows = con.execute(
        "SELECT crowd_level FROM crowd_reports WHERE matched_train_id = ? AND station_code = ? "
        "AND epoch_ms(reported_at) >= ? ORDER BY id",
        [train_id, station_code, oldest_ms],
    ).fetchall()
    return [int(level) for (level,) in rows]
