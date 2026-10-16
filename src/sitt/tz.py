"""The one time zone this project works in."""

from datetime import timedelta, timezone

# Mumbai local time. India has no DST, so a fixed offset is exact and avoids needing
# tzdata on Windows.
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def mumbai_sql(expr: str) -> str:
    """DuckDB SQL for a TIMESTAMPTZ expression as Mumbai wall-clock time (naive TIMESTAMP).

    Casting, `date_trunc` or `hour()` on a TIMESTAMPTZ follow DuckDB's session time zone,
    which is the computer's: IST on the PC, UTC in CI. This uses the fixed offset instead,
    so the same database gives the same days and hours everywhere.
    """
    return f"make_timestamp((epoch_ms({expr}) + 19800000) * 1000)"
