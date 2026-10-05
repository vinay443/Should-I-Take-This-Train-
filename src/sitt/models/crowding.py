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
    inp: CrowdingInput, reports: Sequence[int] = (), rules: CrowdingRules = RULES
) -> CrowdingEstimate:
    """Score one train, blending in the levels of any similar crowd `reports`."""
    from_rules, reasons = rule_score(inp, rules)
    blended = from_rules
    mean = None
    if reports:
        mean = sum(reports) / len(reports)
        weight = len(reports) / (len(reports) + rules.report_half_weight)
        blended = (1 - weight) * from_rules + weight * mean
        plural = "s" if len(reports) != 1 else ""
        reasons.append(f"your {len(reports)} report{plural} average {mean:.1f}")
    score = int(min(5, max(1, math.ceil(blended - 0.5))))
    return CrowdingEstimate(
        score=score,
        label=CROWD_LABELS[score],
        reason=", ".join(reasons),
        rule_score=from_rules,
        reports_used=len(reports),
        reports_mean=mean,
    )


_DESCRIPTION_RE = re.compile(r"(\d{1,2}):(\d{2}) (fast|slow) from (\S+)")


def similar_reports(
    con: duckdb.DuckDBPyConnection,
    station_code: str,
    train_type: str,
    departure: datetime,
    now: datetime | None = None,
    rules: CrowdingRules = RULES,
) -> list[int]:
    """Crowd levels the rider logged for trains like this one. See the module docstring."""
    now = local_naive(now) if now else datetime.now()
    oldest = now - timedelta(days=rules.report_max_age_days)
    rows = con.execute(
        "SELECT crowd_level, train_description, epoch_ms(reported_at) FROM crowd_reports "
        "WHERE station_code = ? AND train_description IS NOT NULL",
        [station_code],
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
