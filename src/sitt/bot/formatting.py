"""User-facing text. No Telegram imports here."""

from collections.abc import Sequence
from datetime import datetime

from sitt.bot.flow import IST, LogDraft
from sitt.bot.parsing import CROWD_LEVELS
from sitt.bot.schedule import Departures
from sitt.bot.stations import STATIONS_BY_CODE
from sitt.bot.storage import StoredReport

HELP_TEXT = """\
I log how crowded your Central line trains are, so we can later predict \
which train is worth taking.

/log 8:12 fast KYN packed: log a trip in one line, in any order. \
Crowd can be 1-5 or empty / seats / standing / packed / can't board.
/log: log a trip step by step with buttons.
/mylogs: your last 10 reports.
/next KYN CSMT: the next scheduled trains between two stations.
/cancel: abandon a log in progress.
/help: show this message."""

NEXT_USAGE = "Tell me where from and to, e.g. /next KYN CSMT or /next Thane Dadar."
NEXT_FOOTER = "Timetable times only. Delay and crowd predictions are coming later."


def crowd_label(level: int) -> str:
    return f"{level}/5 {CROWD_LEVELS[level]}"


def station_label(code: str) -> str:
    station = STATIONS_BY_CODE.get(code)
    if station is None or station.name == code:
        return code
    return f"{station.name} ({code})"


def describe_draft(draft: LogDraft) -> str:
    """One-line summary of the fields filled in so far."""
    parts = []
    if draft.departure_time is not None:
        parts.append(f"{draft.departure_time:%H:%M}")
    if draft.service is not None:
        parts.append(draft.service)
    if draft.station_code is not None:
        parts.append(f"from {station_label(draft.station_code)}")
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


def format_departures(departures: Departures, now: datetime) -> str:
    """Scheduled trains for `/next`. Trains on a later day than `now` show the weekday."""
    route = f"{departures.origin} → {departures.destination}"
    if not departures.trips:
        return f"No scheduled trains {route} today or tomorrow.\n\n{NEXT_FOOTER}"
    lines = [f"Next trains {route}:"]
    for trip in departures.trips:
        day = "" if trip.departure.date() == now.date() else f"{trip.departure:%a} "
        minutes = round(trip.duration.total_seconds() / 60)
        lines.append(
            f"• {day}{trip.departure:%H:%M} {trip.train_type} to {trip.label}, "
            f"arrives {trip.arrival:%H:%M} ({minutes} min)"
        )
    lines += ["", NEXT_FOOTER]
    return "\n".join(lines)
