"""Take this train, or wait for the next one?

`recommend` looks at the next few trains between two stations, predicts when each will
really arrive and how crowded it will be, and picks one, with a one-line reason.

How late each train will be comes from the best source available, and every reply says
which one was used:

    model      the saved LightGBM delay model (sitt.models.delay), if there is one
    baseline   else the median delay seen before for that train at that station, if the
               database has observations
    timetable  else the scheduled times as they stand

A model trained on synthetic (invented) data is **not used** unless
`RecommendSettings.allow_synthetic_model` is set (`SITT_ALLOW_SYNTHETIC_MODEL=true`),
which is for testing. Without it such a model is ignored and the next level down is
used, so by default real trains never get predictions learned from invented delays.
When a synthetic model is allowed, or the observations behind a baseline are synthetic,
the recommendation is flagged `synthetic` and every reply must say so.

The decision rule (`choose`), with thresholds from `sitt.config.RecommendSettings`:

1. Trains reported cancelled are out. So are ladies' specials, unless
   `ladies_special_ok` is set: the recommender doesn't know who is asking, and most
   riders can't board one. They are still listed.
2. Start with the train predicted to arrive first.
3. If that train is very late at the boarding station (`very_late_minutes` or more), and
   another train that isn't very late arrives within `very_late_slack_minutes` of it,
   prefer the other: a badly delayed train's arrival time is the least certain.
4. If another train (leaving earlier or later) arrives within `wait_max_extra_minutes`
   of the pick and is at least `crowd_gain_levels` less crowded, prefer it. Among
   several, the emptiest, then the earliest to arrive.

Crowding is the rule-of-thumb score from `sitt.models.crowding`.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from sitt.config import DEFAULT_MODEL_DIR, RecommendSettings
from sitt.holidays import is_sunday_schedule
from sitt.models import features
from sitt.models.crowding import CrowdingEstimate, CrowdingInput, estimate, similar_reports
from sitt.models.delay import DelayModel, DelayPrediction, load_if_present, predict_targets
from sitt.models.features import Target, local_naive
from sitt.timetable import ScheduledTrip, next_trains, resolve_station

LEVEL_MODEL, LEVEL_BASELINE, LEVEL_TIMETABLE = "model", "baseline", "timetable"
SYNTHETIC_SOURCE = "synthetic"
# How far back to look for the train that left just before the first candidate.
AHEAD_LOOKBACK_MINUTES = 45
# A cancellation reading counts if it was made no longer than this before `now`.
CANCELLATION_MAX_AGE_HOURS = 4
MIN_BASELINE_READINGS_FOR_RANGE = 5

LEVEL_DESCRIPTIONS = {
    LEVEL_MODEL: "delay model",
    LEVEL_BASELINE: "typical past delay of each train",
    LEVEL_TIMETABLE: "timetable only, no delay data",
}


@dataclass
class Option:
    """One candidate train, with what is predicted for it."""

    trip: ScheduledTrip
    departure_delay: float = 0.0  # predicted minutes late leaving the boarding station
    arrival_delay: float = 0.0  # predicted minutes late at the destination
    arrival_low: float | None = None  # rough 10th-90th percentile range of arrival delay
    arrival_high: float | None = None
    cancelled: bool = False
    starts_here: bool = False
    crowding: CrowdingEstimate | None = None

    @property
    def predicted_departure(self) -> datetime:
        return self.trip.departure + timedelta(minutes=round(self.departure_delay))

    @property
    def predicted_arrival(self) -> datetime:
        return self.trip.arrival + timedelta(minutes=round(self.arrival_delay))

    @property
    def arrival_margin(self) -> int | None:
        """Half the width of the predicted range, in whole minutes, for "±4 min"."""
        if self.arrival_low is None or self.arrival_high is None:
            return None
        return max(1, round((self.arrival_high - self.arrival_low) / 2))

    @property
    def crowd_score(self) -> int:
        return self.crowding.score if self.crowding else 3

    @property
    def name(self) -> str:
        return f"{self.trip.departure:%H:%M} {self.trip.train_type}"


@dataclass
class Recommendation:
    origin: str
    destination: str
    asked_at: datetime
    options: list[Option]
    choice: int | None  # index into options, or None when nothing can be recommended
    reason: str  # the one-line recommendation
    rule: str  # which rule decided it: earliest, crowding, very_late, only, none
    level: str  # model, baseline or timetable
    synthetic: bool = False
    notes: list[str] = field(default_factory=list)
    settings: RecommendSettings = field(default_factory=RecommendSettings)

    @property
    def chosen(self) -> Option | None:
        return None if self.choice is None else self.options[self.choice]

    @property
    def level_text(self) -> str:
        text = LEVEL_DESCRIPTIONS[self.level]
        if self.synthetic:
            text += ", built from SYNTHETIC (invented) data, not real trains"
        return text


# --- The decision rule --------------------------------------------------------------------


def _minutes_between(later: datetime, earlier: datetime) -> int:
    return round((later - earlier).total_seconds() / 60)


def _arrival_text(option: Option) -> str:
    text = f"arrives {option.predicted_arrival:%H:%M}"
    if option.arrival_margin is not None:
        text += f" ±{option.arrival_margin} min"
    return text


def choose(options: list[Option], settings: RecommendSettings) -> tuple[int | None, str, str]:
    """Apply the decision rule. Returns (index of the pick, one-line reason, rule name)."""
    if not options:
        return None, "No trains found in the timetable for the next day.", "none"
    not_cancelled = [i for i, option in enumerate(options) if not option.cancelled]
    if not not_cancelled:
        return (
            None,
            "Every train I can see is reported cancelled. Check the station boards.",
            "none",
        )
    running = [
        i
        for i in not_cancelled
        if settings.ladies_special_ok or not options[i].trip.is_ladies_special
    ]
    if not running:
        return None, "The only trains I can see running are ladies' specials.", "none"

    def arrival(i: int) -> tuple[datetime, datetime]:
        return options[i].predicted_arrival, options[i].trip.departure

    def very_late(i: int) -> bool:
        return options[i].departure_delay >= settings.very_late_minutes

    earliest = min(running, key=arrival)
    pick, rule = earliest, "earliest"

    if very_late(earliest):
        limit = options[earliest].predicted_arrival + timedelta(
            minutes=settings.very_late_slack_minutes
        )
        steadier = [
            i for i in running if not very_late(i) and options[i].predicted_arrival <= limit
        ]
        if steadier:
            pick, rule = min(steadier, key=arrival), "very_late"

    limit = options[pick].predicted_arrival + timedelta(minutes=settings.wait_max_extra_minutes)
    emptier = [
        i
        for i in running
        if i != pick
        and options[i].predicted_arrival <= limit
        and options[i].crowd_score <= options[pick].crowd_score - settings.crowd_gain_levels
        and not very_late(i)
    ]
    crowd_base = pick
    if emptier:
        pick = min(emptier, key=lambda i: (options[i].crowd_score, *arrival(i)))
        rule = "crowding"

    chosen = options[pick]
    first = options[0]
    # "Wait" when the rider would let an earlier train that is running go by.
    waiting = any(i < pick for i in running)
    head = f"{'Wait for' if waiting else 'Take'} the {chosen.name}: {_arrival_text(chosen)}"
    crowd = chosen.crowding.label if chosen.crowding else None

    if rule == "crowding":
        other = options[crowd_base]
        gap = _minutes_between(chosen.predicted_arrival, other.predicted_arrival)
        when = f"{gap} min after" if gap > 0 else "no later than"
        detail = f"{when} the {other.name}, and likely {crowd} rather than {other.crowding.label}"
    elif rule == "very_late":
        late = options[earliest]
        detail = (
            f"the {late.name} is running about {round(late.departure_delay)} min late, "
            "so its arrival is less certain"
        )
    else:
        later = [i for i in running if i != pick]
        if not later:
            rule = "only"
            detail = "it is the only train running that I can see"
        else:
            runner_up = min(later, key=arrival)
            gap = _minutes_between(options[runner_up].predicted_arrival, chosen.predicted_arrival)
            behind = "at the same time" if gap <= 0 else f"{gap} min later"
            if crowd and chosen.crowd_score >= 4:
                next_best = options[runner_up].name
                detail = f"likely {crowd}, but the next best ({next_best}) arrives {behind}"
            elif crowd:
                detail = f"earliest arrival, likely {crowd}"
            else:
                detail = "earliest arrival"
    if first.cancelled and rule != "crowding":
        detail = f"the {first.name} is reported cancelled; {detail}"
    return pick, f"{head}, {detail}.", rule


# --- Predictions ---------------------------------------------------------------------------


def _has_observations(con: duckdb.DuckDBPyConnection) -> tuple[bool, bool]:
    """(any usable observations, any of them synthetic)."""
    rows = con.execute("SELECT DISTINCT source FROM obs").fetchall()
    sources = {row[0] for row in rows}
    return bool(sources), SYNTHETIC_SOURCE in sources


def _baseline_predictions(
    con: duckdb.DuckDBPyConnection, targets: list[Target]
) -> list[DelayPrediction | None]:
    """Median delay seen on earlier days for each train at each station."""
    out: list[DelayPrediction | None] = []
    for target in targets:
        median, low, high, count = con.execute(
            "SELECT median(delay_minutes), quantile_cont(delay_minutes, 0.1), "
            "quantile_cont(delay_minutes, 0.9), count(*) FROM obs "
            "WHERE train_id = ? AND station_code = ? AND service_day < ?",
            [target.train_id, target.station_code, target.service_day],
        ).fetchone()
        if not count:
            out.append(None)
        elif count < MIN_BASELINE_READINGS_FOR_RANGE:
            out.append(DelayPrediction(float(median), float(median), float(median)))
        else:
            out.append(DelayPrediction(float(median), float(low), float(high)))
    return out


def _cancelled_trains(con: duckdb.DuckDBPyConnection, now: datetime) -> set[str]:
    since = now - timedelta(hours=CANCELLATION_MAX_AGE_HOURS)
    rows = con.execute(
        f"""
        SELECT DISTINCT train_id FROM observations
        WHERE coalesce(cancelled, false)
          AND make_timestamp((epoch_ms(observed_at) + {features.IST_OFFSET_MS}) * 1000)
              BETWEEN ? AND ?
        """,
        [since, now],
    ).fetchall()
    return {row[0] for row in rows}


def _origins(con: duckdb.DuckDBPyConnection, train_ids: list[str]) -> dict[str, str]:
    """The first station of each train."""
    if not train_ids:
        return {}
    rows = con.execute(
        "SELECT train_id, arg_min(station_code, stop_seq) FROM scheduled_stops "
        "WHERE train_id IN (SELECT unnest(?)) GROUP BY train_id",
        [train_ids],
    ).fetchall()
    return dict(rows)


def recommend(
    con: duckdb.DuckDBPyConnection,
    origin: str,
    destination: str,
    now: datetime,
    settings: RecommendSettings | None = None,
    model: DelayModel | None = None,
    model_dir: Path | str | None = DEFAULT_MODEL_DIR,
) -> Recommendation:
    """Compare the next trains from `origin` to `destination` and recommend one.

    Stations are codes or names in the timetable. `model` is the delay model to use;
    if it is None, the one saved in `model_dir` is loaded when there is one (pass
    `model_dir=None` to use no model). Either way, a model trained on synthetic data is
    ignored unless `settings.allow_synthetic_model` is set. Raises what `next_trains`
    raises for unknown stations.
    """
    settings = settings or RecommendSettings()
    local_now = local_naive(now)
    trips = next_trains(con, origin, destination, local_now, n=settings.candidates)
    earlier = [
        trip
        for trip in next_trains(
            con,
            origin,
            destination,
            local_now - timedelta(minutes=AHEAD_LOOKBACK_MINUTES),
            n=settings.candidates * 3,
        )
        if trip.departure < local_now
    ]
    names = dict(con.execute("SELECT code, name FROM stations").fetchall())
    origin_code = resolve_station(con, origin)
    destination_code = resolve_station(con, destination)

    features.prepare(con)
    has_observations, observations_synthetic = _has_observations(con)
    if model is None and model_dir is not None:
        model = load_if_present(Path(model_dir))
    notes: list[str] = []
    if model is not None and model.synthetic and not settings.allow_synthetic_model:
        model = None
        notes.append("A delay model exists but was trained on synthetic data, so it is not used.")

    # The train just ahead of the first candidate matters for crowding, so predict it too.
    ahead_of_first = earlier[-1] if earlier else None
    everything = ([ahead_of_first] if ahead_of_first else []) + trips
    targets = []
    for trip in everything:
        day = trip.service_day or trip.departure.date()
        targets.append(Target(trip.train_id, day, origin_code, local_now))
        targets.append(Target(trip.train_id, day, destination_code, local_now))

    if model is not None:
        level, synthetic = LEVEL_MODEL, model.synthetic
        predictions = predict_targets(con, model, targets)
    elif has_observations:
        level, synthetic = LEVEL_BASELINE, observations_synthetic
        predictions = _baseline_predictions(con, targets)
        if any(p is None for p in predictions):
            notes.append("Some trains have no past readings; they are shown at timetable times.")
    else:
        level, synthetic = LEVEL_TIMETABLE, False
        predictions = [None] * len(targets)

    cancelled = _cancelled_trains(con, local_now)
    starts = _origins(con, [trip.train_id for trip in everything])

    def option_for(position: int, trip: ScheduledTrip) -> Option:
        at_origin, at_destination = predictions[2 * position], predictions[2 * position + 1]
        option = Option(
            trip=trip,
            cancelled=trip.train_id in cancelled,
            starts_here=starts.get(trip.train_id) == origin_code,
        )
        if at_origin is not None:
            option.departure_delay = at_origin.minutes
        if at_destination is not None:
            option.arrival_delay = at_destination.minutes
            if level != LEVEL_TIMETABLE and at_destination.high > at_destination.low:
                option.arrival_low, option.arrival_high = at_destination.low, at_destination.high
        return option

    all_options = [option_for(i, trip) for i, trip in enumerate(everything)]
    options = all_options[1:] if ahead_of_first else all_options
    for i, option in enumerate(options):
        before = all_options[i] if ahead_of_first else (options[i - 1] if i else None)
        trip = option.trip
        day = trip.service_day or trip.departure.date()
        option.crowding = estimate(
            CrowdingInput(
                direction=trip.direction,
                train_type=trip.train_type,
                departure=trip.departure,
                starts_here=option.starts_here,
                car_count=trip.car_count,
                runs_ac=trip.runs_ac,
                is_ladies_special=trip.is_ladies_special,
                sunday_schedule=is_sunday_schedule(day),
                ahead_cancelled=bool(before and before.cancelled),
                # Without delay data nothing is known about the train ahead.
                ahead_delay=before.departure_delay if before and level != LEVEL_TIMETABLE else None,
            ),
            similar_reports(con, origin_code, trip.train_type, trip.departure, local_now),
        )

    choice, reason, rule = choose(options, settings)
    return Recommendation(
        origin=names.get(origin_code, origin_code),
        destination=names.get(destination_code, destination_code),
        asked_at=local_now,
        options=options,
        choice=choice,
        reason=reason,
        rule=rule,
        level=level,
        synthetic=synthetic,
        notes=notes,
        settings=settings,
    )


# --- Explaining it -------------------------------------------------------------------------

RULE_EXPLANATIONS = {
    "earliest": "It is predicted to arrive first, and no later train is both nearly as quick "
    "and less crowded.",
    "crowding": "Another train arrives almost as soon and should be less crowded, so it is "
    "worth the few extra minutes.",
    "very_late": "The train that would arrive first is running very late, which makes its "
    "arrival time unreliable, and another train arrives soon after.",
    "only": "It is the only train I can see that is running.",
    "none": "Nothing could be recommended.",
}


def _delay_text(minutes: float) -> str:
    whole = round(minutes)
    if whole == 0:
        return "on time"
    return f"{abs(whole)} min {'late' if whole > 0 else 'early'}"


def explain(recommendation: Recommendation) -> str:
    """A fuller account of a recommendation, for `/why`."""
    rec = recommendation
    lines = [
        f"{rec.origin} → {rec.destination}, asked at {rec.asked_at:%H:%M on %a %d %b}.",
        "",
        rec.reason,
        RULE_EXPLANATIONS[rec.rule],
        "",
        f"Predictions used: {rec.level_text}.",
    ]
    if rec.level == LEVEL_TIMETABLE:
        lines.append(
            "There is no delay model and no past observations, so every train is assumed "
            "to run on time."
        )
    if rec.synthetic:
        lines.append(
            "SYNTHETIC: the delay figures come from invented data and say nothing about how "
            "real trains run. Treat only the timetable times as real."
        )
    lines += rec.notes
    lines.append("")
    for i, option in enumerate(rec.options):
        trip = option.trip
        marker = "➜" if i == rec.choice else "•"
        lines.append(f"{marker} {option.name} to {trip.label} ({trip.number})")
        if option.cancelled:
            lines.append("    reported CANCELLED")
            continue
        lines.append(
            f"    leaves {trip.departure:%H:%M} scheduled, predicted "
            f"{option.predicted_departure:%H:%M} ({_delay_text(option.departure_delay)})"
        )
        arrival = (
            f"    arrives {trip.arrival:%H:%M} scheduled, predicted "
            f"{option.predicted_arrival:%H:%M} ({_delay_text(option.arrival_delay)}"
        )
        if option.arrival_low is not None:
            arrival += (
                f", likely between {_delay_text(option.arrival_low)} and "
                f"{_delay_text(option.arrival_high)}"
            )
        lines.append(arrival + ")")
        if option.crowding:
            lines.append(
                f"    crowding {option.crowding.score}/5 {option.crowding.label}: "
                f"{option.crowding.reason}"
            )
    s = rec.settings
    lines += [
        "",
        "Rules: take the earliest predicted arrival"
        + ("" if s.ladies_special_ok else " (ladies' specials are never recommended)")
        + ". Wait for a later train if it arrives "
        f"within {s.wait_max_extra_minutes:g} min of it and is at least "
        f"{s.crowd_gain_levels} crowding level(s) emptier. Pass over a train running "
        f"{s.very_late_minutes:g}+ min late if another arrives within "
        f"{s.very_late_slack_minutes:g} min. Crowding is a rule-of-thumb estimate, not a "
        "measurement.",
    ]
    return "\n".join(lines)
