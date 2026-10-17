"""User-facing text. No Telegram imports here."""

from collections.abc import Sequence
from datetime import datetime

from sitt.bot.flow import LogDraft
from sitt.bot.parsing import CROWD_LEVELS
from sitt.bot.stations import FALLBACK_DIRECTORY, StationDirectory
from sitt.bot.storage import StoredReport
from sitt.recommend import LEVEL_TIMETABLE, Recommendation, part_requests
from sitt.timetable import ScheduledTrip
from sitt.tz import IST

HELP_TEXT = """\
I log how crowded your Central line trains are, so we can later predict \
which train is worth taking.

/log 8:12 fast KYN packed: log a trip in one line, in any order. \
Crowd can be 1-5 or empty / seats / standing / packed / can't board.
/log: log a trip step by step with buttons.
/mylogs: your last 10 reports.
/next KYN CSMT: which of the next trains to take, with predicted arrival and \
crowding for each. Add a time or day to plan ahead: /next KYN CSMT 8:00, \
/next KYN CSMT tomorrow 8:12. Add "live" to fetch live running status first: \
/next KYN CSMT live.
/why: explain the last /next recommendation.
/fav add work KYN CSMT 8:12 mon-fri: save a route and your usual train. \
/fav on its own lists them.
/commute: /next for your saved route. With a route saved both ways, it gives the \
return leg in the afternoon.
/cancel: abandon a log in progress.
/help: show this message."""

NEXT_USAGE = (
    "Tell me where from and to, e.g. /next KYN CSMT or /next Kanjur Marg Thane. "
    "Add a time to plan ahead: /next KYN CSMT 8:00 or /next KYN CSMT tomorrow 8:00."
)
NO_RECOMMENDATION_YET = "Nothing to explain yet. Ask with /next first, e.g. /next KYN CSMT."
CROWDING_CAVEAT = "Crowding is a rule-of-thumb estimate."


def crowd_label(level: int) -> str:
    return f"{level}/5 {CROWD_LEVELS[level]}"


def describe_draft(draft: LogDraft, stations: StationDirectory = FALLBACK_DIRECTORY) -> str:
    """One-line summary of the fields filled in so far."""
    parts = []
    if draft.departure_time is not None:
        parts.append(f"{draft.departure_time:%H:%M}")
    if draft.service is not None:
        parts.append(draft.service)
    if draft.station_code is not None:
        parts.append(f"from {stations.label(draft.station_code)}")
    summary = " ".join(parts)
    if draft.crowd_level is None:
        return summary
    return " · ".join(filter(None, [summary, crowd_label(draft.crowd_level)]))


def format_report(report: StoredReport) -> str:
    day = f"{report.reported_at.astimezone(IST):%a %d %b}"
    train = report.train_description or report.station_code or "unknown train"
    return f"{day} · {train} · {crowd_label(report.crowd_level)}"


def format_report_list(reports: Sequence[StoredReport]) -> str:
    if not reports:
        return "No reports yet. Log one with /log."
    lines = [f"Your last {len(reports)} report{'s' if len(reports) != 1 else ''}:"]
    lines += (f"• {format_report(report)}" for report in reports)
    return "\n".join(lines)


def trip_tags(trip: ScheduledTrip) -> list[str]:
    """What a rider should know before boarding: who may board, AC fare, rake length."""
    tags = []
    if trip.is_ladies_special:
        tags.append("LADIES SPECIAL (women only)")
    if trip.runs_ac:
        tags.append("AC")
    if trip.car_count == 15:
        tags.append("15-car")
    return tags


def prediction_footer(recommendation: Recommendation) -> str:
    """Says what the times are based on. Always names synthetic data when it was used."""
    live = any(option.live is not None for option in recommendation.options)
    if recommendation.level == LEVEL_TIMETABLE:
        basis = (
            "Timetable times, except trains marked live (a reading from the last few minutes)."
            if live
            else "Timetable times only: there is no delay data yet."
        )
        if recommendation.synthetic:
            basis += " The live readings are SYNTHETIC (invented), not real trains."
    else:
        basis = f"Arrival times use the {recommendation.level_text}."
        if live:
            basis += " Trains marked live use a reading from the last few minutes."
    return f"{basis} {CROWDING_CAVEAT} /why for details."


def format_recommendation(
    recommendation: Recommendation, show_time: bool = False, now: datetime | None = None
) -> str:
    """The `/next` reply: the recommendation, then each train with its prediction.

    `show_time` adds when the question is about, for a rider who asked about a time other
    than now.
    """
    rec = recommendation
    header = f"{rec.origin} → {rec.destination}"
    if show_time:
        header += f", at {rec.asked_at:%H:%M on %a %d %b}"
    lines = [header, "", rec.reason, ""]
    # Each part of a journey with a change, as a command to send. The bot reads a day word
    # from the time the message is sent, so the day is always written, counted from now
    # (not from the time asked about); a part beyond tomorrow gets no command.
    today = (now or datetime.now(IST)).replace(tzinfo=None).date()
    asks = []
    for a, b, when in part_requests(rec):
        days = (when.replace(tzinfo=None).date() - today).days
        if days in (0, 1):
            asks.append(f"/next {a} {b} {'tomorrow' if days else 'today'} {when:%H:%M}")
    if asks:
        lines += ["Ask each part:", *asks, ""]
    for i, option in enumerate(rec.options):
        trip = option.trip
        day = "" if trip.departure.date() == rec.asked_at.date() else f"{trip.departure:%a} "
        marker = "➜" if i == rec.choice else "•"
        line = f"{marker} {day}{trip.departure:%H:%M} {trip.train_type} to {trip.label}"
        if option.cancelled:
            line += " · CANCELLED"
        else:
            line += f" · arr {option.predicted_arrival:%H:%M}"
            late = round(option.arrival_delay)
            if late:
                line += f" ({abs(late)} min {'late' if late > 0 else 'early'})"
            if option.live is not None:
                line += " · live"
            if option.crowding:
                line += f" · {option.crowding.label}"
        if tags := trip_tags(trip):
            line += f" · {' · '.join(tags)}"
        lines.append(line)
    if rec.options:
        lines.append("")
    lines += [*rec.notes, prediction_footer(rec)]
    return "\n".join(lines)
