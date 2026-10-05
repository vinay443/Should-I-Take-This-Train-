"""The one time zone this project works in."""

from datetime import timedelta, timezone

# Mumbai local time. India has no DST, so a fixed offset is exact and avoids needing
# tzdata on Windows.
IST = timezone(timedelta(hours=5, minutes=30), "IST")
