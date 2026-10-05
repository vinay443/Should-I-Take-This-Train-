"""Days on which Mumbai suburban trains run to the Sunday timetable.

Central Railway's list (the "list of holidays" PDF beside the timetables) has six
fixed dates and seven festivals whose dates move each year (Holi, Gudi Padwa, Good
Friday, Ramzan Id, Ganesh Chaturthi, Dussehra, two days of Diwali). The fixed dates
are here. The movable ones are in `holidays.toml` beside this module, one entry per
date with where it came from, so they can be corrected or extended without touching
code.

Each movable date has a status. "confirmed" and "reported" dates are used. A
"tentative" date (a year the state hasn't notified yet) is used only when
`SITT_HOLIDAYS_INCLUDE_TENTATIVE=true`, because it may be a day out.
"""

import os
import tomllib
from dataclasses import dataclass
from datetime import date
from importlib.resources import files

# (month, day): Republic Day, Ambedkar Jayanti, Maharashtra Day, Independence Day,
# Gandhi Jayanti, Christmas.
FIXED_HOLIDAYS: frozenset[tuple[int, int]] = frozenset(
    {(1, 26), (4, 14), (5, 1), (8, 15), (10, 2), (12, 25)}
)

CONFIRMED, REPORTED, TENTATIVE = "confirmed", "reported", "tentative"
STATUSES = (CONFIRMED, REPORTED, TENTATIVE)
INCLUDE_TENTATIVE_VARIABLE = "SITT_HOLIDAYS_INCLUDE_TENTATIVE"


class HolidayFileError(ValueError):
    """`holidays.toml` is wrong. The message says which entry and why."""


@dataclass(frozen=True)
class MovableHoliday:
    date: date
    name: str
    status: str
    source: str
    note: str = ""

    @property
    def used(self) -> bool:
        """Whether this date counts as a holiday without the tentative switch."""
        return self.status != TENTATIVE


def parse_holidays(text: str, source: str = "holidays.toml") -> tuple[MovableHoliday, ...]:
    """Read the movable holidays from TOML text. Raises HolidayFileError on any mistake."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise HolidayFileError(f"{source} is not valid TOML: {exc}") from None
    holidays = []
    problems = []
    seen: dict[date, int] = {}
    for index, raw in enumerate(data.get("holiday", []), start=1):
        where = f"entry {index}"
        day = raw.get("date")
        if type(day) is not date:
            problems.append(f"{where}: date must be written like 2026-10-20, without quotes")
            continue
        where = f"{where} ({day.isoformat()})"
        for key in ("name", "status", "source"):
            if not (isinstance(raw.get(key), str) and raw[key].strip()):
                problems.append(f"{where}: {key} is required")
        if raw.get("status") not in STATUSES and isinstance(raw.get("status"), str):
            problems.append(f"{where}: status must be one of {', '.join(STATUSES)}")
        for key in sorted(set(raw) - {"date", "name", "status", "source", "note"}):
            problems.append(f"{where}: unknown key {key!r}")
        if day in seen:
            problems.append(f"{where}: the same date as entry {seen[day]}")
        seen[day] = index
        if (day.month, day.day) in FIXED_HOLIDAYS:
            problems.append(f"{where}: already a fixed holiday; it doesn't need an entry")
        holidays.append(
            MovableHoliday(
                day,
                str(raw.get("name", "")),
                str(raw.get("status", "")),
                str(raw.get("source", "")),
                str(raw.get("note", "")),
            )
        )
    if problems:
        raise HolidayFileError(
            f"{source} has {len(problems)} problem(s):\n  " + "\n  ".join(problems)
        )
    return tuple(sorted(holidays, key=lambda holiday: holiday.date))


def load_holidays() -> tuple[MovableHoliday, ...]:
    """The movable holidays shipped with the package."""
    text = files("sitt").joinpath("holidays.toml").read_text(encoding="utf-8")
    return parse_holidays(text)


MOVABLE: tuple[MovableHoliday, ...] = load_holidays()

# Dates of the movable holidays that are used by default (confirmed or reported).
MOVABLE_HOLIDAYS: frozenset[date] = frozenset(h.date for h in MOVABLE if h.used)
TENTATIVE_HOLIDAYS: frozenset[date] = frozenset(h.date for h in MOVABLE if not h.used)


def include_tentative() -> bool:
    return (os.environ.get(INCLUDE_TENTATIVE_VARIABLE) or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def movable_dates(tentative: bool | None = None) -> frozenset[date]:
    """The movable holiday dates in force. `tentative` defaults to the environment switch."""
    if tentative is None:
        tentative = include_tentative()
    return MOVABLE_HOLIDAYS | TENTATIVE_HOLIDAYS if tentative else MOVABLE_HOLIDAYS


def is_holiday(day: date) -> bool:
    return (day.month, day.day) in FIXED_HOLIDAYS or day in movable_dates()


def is_sunday_schedule(day: date) -> bool:
    """Whether trains run to the Sunday timetable on `day`: a Sunday or a listed holiday."""
    return day.weekday() == 6 or is_holiday(day)


def years_covered() -> dict[int, str]:
    """Per year with movable entries: 'complete' or 'tentative' (some dates not yet notified)."""
    out: dict[int, str] = {}
    for holiday in MOVABLE:
        worst = out.get(holiday.date.year, "complete")
        out[holiday.date.year] = "tentative" if not holiday.used else worst
    return out
