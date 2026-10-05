"""Days on which Mumbai suburban trains run to the Sunday timetable.

Central Railway's list (the "list of holidays" PDF beside the timetables) has six
fixed dates and seven festivals whose dates move each year (Holi, Gudi Padwa, Good
Friday, Ramzan Id, Ganesh Chaturthi, Dussehra, two days of Diwali). Only the fixed
dates are known here. Add a year's movable dates to `MOVABLE_HOLIDAYS` when the
railway announces them.
"""

from datetime import date

# (month, day): Republic Day, Ambedkar Jayanti, Maharashtra Day, Independence Day,
# Gandhi Jayanti, Christmas.
FIXED_HOLIDAYS: frozenset[tuple[int, int]] = frozenset(
    {(1, 26), (4, 14), (5, 1), (8, 15), (10, 2), (12, 25)}
)

# Dates of the movable holidays, per year, once announced. Empty until someone fills it.
MOVABLE_HOLIDAYS: frozenset[date] = frozenset()


def is_holiday(day: date) -> bool:
    return (day.month, day.day) in FIXED_HOLIDAYS or day in MOVABLE_HOLIDAYS


def is_sunday_schedule(day: date) -> bool:
    """Whether trains run to the Sunday timetable on `day`: a Sunday or a listed holiday."""
    return day.weekday() == 6 or is_holiday(day)
