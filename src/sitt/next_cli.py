"""`sitt-next`: "should I take this train?" from the command line, no Telegram needed.

    uv run sitt-next KYN CSMT                    the next trains from now
    uv run sitt-next KYN CSMT --at 08:00         as if asked at 08:00 today
    uv run sitt-next Kalyan CSMT --date 2026-10-13 --at 18:30
    uv run sitt-next KYN CSMT --train 08:12      focus on the train leaving at 08:12
    uv run sitt-next KYN CSMT --why              the full explanation, as /why gives it

It uses the same recommender as the bot's `/next` (sitt.recommend), on the database at
SITT_DB_PATH or `--db`. Nothing is fetched from the network. To use a fresh live reading,
run `uv run sitt-collect` once by hand first (see docs/recommender.md).

Exit codes: 0 a recommendation was printed, 1 nothing could be recommended or the asked-for
train isn't in the timetable, 2 bad arguments (unknown station, bad time, no database).
"""

import argparse
import sys
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sitt.bot.formatting import trip_tags
from sitt.bot.schedule import ScheduleError, recommend_trains
from sitt.config import load_recommend_settings, load_settings
from sitt.recommend import LEVEL_TIMETABLE, Option, Recommendation, explain
from sitt.tz import IST

# With --train and no --at, the question is asked this long before the train leaves, so
# that it is the first candidate however busy the hour (the recommender still looks at
# the train just ahead of it for crowding).
TRAIN_LEAD_MINUTES = 1


def _clock(text: str) -> time:
    try:
        hours, minutes = text.strip().replace(".", ":").split(":")
        return time(int(hours), int(minutes))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a time: {text!r} (use HH:MM, e.g. 08:12)") from None


def _day(text: str) -> date:
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date: {text!r} (use YYYY-MM-DD)") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sitt-next",
        description="Which of the next Mumbai local trains to take, with predicted delay "
        "and crowding.",
    )
    parser.add_argument("origin", help="boarding station: code or name, e.g. KYN or Kalyan")
    parser.add_argument("destination", help="destination station, e.g. CSMT")
    parser.add_argument("--at", type=_clock, help="ask as if it were this time (HH:MM)")
    parser.add_argument("--date", type=_day, help="the day to ask about (YYYY-MM-DD)")
    parser.add_argument(
        "--train",
        type=_clock,
        help="the departure time (HH:MM) of a train you are thinking of taking",
    )
    parser.add_argument("--why", action="store_true", help="print the full explanation")
    parser.add_argument("-n", type=int, help="how many trains to compare (default 5)")
    parser.add_argument("--db", type=Path, help="database file (default SITT_DB_PATH)")
    parser.add_argument("--no-model", action="store_true", help="ignore any saved delay model")
    return parser


def asked_at(now: datetime, at: time | None, day: date | None, train: time | None) -> datetime:
    """The moment the question is asked, in Mumbai time."""
    now = now.astimezone(IST)
    day = day or now.date()
    if at is not None:
        return datetime.combine(day, at, IST)
    if train is not None:
        return datetime.combine(day, train, IST) - timedelta(minutes=TRAIN_LEAD_MINUTES)
    if day != now.date():
        return datetime.combine(day, time(0, 0), IST)
    return now


def find_train(recommendation: Recommendation, departs: time) -> int | None:
    for i, option in enumerate(recommendation.options):
        if option.trip.departure.time().replace(second=0, microsecond=0) == departs:
            return i
    return None


def _delay(minutes: float) -> str:
    whole = round(minutes)
    if whole == 0:
        return "on time"
    return f"{abs(whole)} min {'late' if whole > 0 else 'early'}"


def _row(option: Option, chosen: bool, focused: bool, asked: datetime) -> list[str]:
    trip = option.trip
    day = "" if trip.departure.date() == asked.date() else f"{trip.departure:%a} "
    marker = ">" if chosen else ("*" if focused else " ")
    if option.cancelled:
        arrival, delay, crowd = "CANCELLED", "", ""
    else:
        arrival = f"{option.predicted_arrival:%H:%M}"
        if option.arrival_margin is not None:
            arrival += f" ±{option.arrival_margin}"
        delay = _delay(option.departure_delay)
        crowd = f"{option.crowding.score}/5 {option.crowding.label}" if option.crowding else "?"
    return [
        marker,
        f"{day}{trip.departure:%H:%M}",
        f"{trip.train_type} to {trip.label}",
        arrival,
        delay,
        crowd,
        ", ".join(trip_tags(trip)),
    ]


def format_table(recommendation: Recommendation, focus: int | None = None) -> str:
    header = ["", "Leaves", "Train", "Arrives", "Leaving", "Crowding", "Tags"]
    rows = [
        _row(option, i == recommendation.choice, i == focus, recommendation.asked_at)
        for i, option in enumerate(recommendation.options)
    ]
    widths = [max(len(r[c]) for r in [header, *rows]) for c in range(len(header))]
    lines = []
    for r in [header, *rows]:
        lines.append("  ".join(cell.ljust(w) for cell, w in zip(r, widths, strict=True)).rstrip())
    return "\n".join(lines)


def format_focus(recommendation: Recommendation, index: int) -> str:
    """A few lines about the one train the rider asked about."""
    option = recommendation.options[index]
    trip = option.trip
    lines = [f"The {option.name} to {trip.label} ({trip.number}):"]
    if option.cancelled:
        lines.append("  reported CANCELLED")
        return "\n".join(lines)
    lines.append(
        f"  leaves {trip.departure:%H:%M} scheduled, predicted "
        f"{option.predicted_departure:%H:%M} ({_delay(option.departure_delay)})"
    )
    arrival = (
        f"  arrives {trip.arrival:%H:%M} scheduled, predicted "
        f"{option.predicted_arrival:%H:%M} ({_delay(option.arrival_delay)}"
    )
    if option.arrival_low is not None:
        arrival += (
            f", likely between {_delay(option.arrival_low)} and {_delay(option.arrival_high)}"
        )
    lines.append(arrival + ")")
    if option.crowding:
        lines.append(
            f"  crowding {option.crowding.score}/5 {option.crowding.label}: "
            f"{option.crowding.reason}"
        )
    if index == recommendation.choice:
        lines.append("  This is the recommended train.")
    elif recommendation.chosen is not None:
        lines.append(f"  Recommended instead: the {recommendation.chosen.name}.")
    return "\n".join(lines)


def format_answer(recommendation: Recommendation, focus: int | None = None) -> str:
    rec = recommendation
    lines = [
        f"{rec.origin} -> {rec.destination}, asked at {rec.asked_at:%H:%M on %a %d %b %Y}",
        "",
        rec.reason,
        "",
    ]
    if rec.options:
        lines += [format_table(rec, focus), ""]
    if focus is not None:
        lines += [format_focus(rec, focus), ""]
    if rec.level == LEVEL_TIMETABLE:
        lines.append("Times: timetable only, there is no delay data yet.")
    else:
        lines.append(f"Times: {rec.level_text}.")
    if rec.synthetic:
        lines.append("SYNTHETIC: these delays come from invented data, not real trains.")
    lines += rec.notes
    lines.append("Crowding is a rule-of-thumb estimate. Add --why for the full explanation.")
    return "\n".join(lines)


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    # Windows consoles and redirected output may not be UTF-8; never crash on a symbol.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings()
    db_path = args.db or settings.db_path
    if not Path(db_path).exists():
        print(
            f"No database at {db_path}. Run `uv run sitt-init-db` and load a timetable "
            "(see the README).",
            file=sys.stderr,
        )
        return 2
    recommend_settings = load_recommend_settings()
    if args.n is not None:
        recommend_settings = replace(recommend_settings, candidates=max(1, args.n))
    when = asked_at(now or datetime.now(IST), args.at, args.date, args.train)
    try:
        recommendation = recommend_trains(
            db_path,
            args.origin,
            args.destination,
            when,
            settings=recommend_settings,
            model_dir=None if args.no_model else settings.model_dir,
        )
    except ScheduleError as exc:
        print(exc, file=sys.stderr)
        return 2

    focus = None
    if args.train is not None:
        focus = find_train(recommendation, args.train)
        if focus is None:
            listed = ", ".join(o.name for o in recommendation.options) or "none"
            print(
                f"No train leaves {recommendation.origin} at {args.train:%H:%M} for "
                f"{recommendation.destination} in the timetable. Trains around then: {listed}.",
                file=sys.stderr,
            )
            return 1

    print(explain(recommendation) if args.why else format_answer(recommendation, focus))
    return 0 if recommendation.choice is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
