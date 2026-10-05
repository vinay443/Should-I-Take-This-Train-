from datetime import date

from sitt.holidays import is_holiday, is_sunday_schedule
from sitt.routes import load_routes, stage_route_points


def test_fast_train_gets_pass_points_at_skipped_stations(loaded):
    routes = {route.number: route for route in load_routes(loaded)}
    fast = routes["90106"]  # KYN 07:12, DI 07:18, TNA 07:33, ... CSMT 08:19
    codes = [p.station_code for p in fast.points]
    assert codes[:4] == ["KYN", "THK", "DI", "KOPR"]
    assert codes[-1] == "CSMT" and len(codes) == 26  # every station from Kalyan to CSMT
    by_code = {p.station_code: p for p in fast.points}
    assert (by_code["KYN"].minutes, by_code["KYN"].is_stop) == (0.0, True)
    assert (by_code["THK"].minutes, by_code["THK"].is_stop) == (3.5, False)  # halfway to DI
    assert (by_code["DI"].minutes, by_code["DI"].is_stop) == (7.0, True)  # departs 07:19
    assert fast.duration == 67.0
    minutes = [p.minutes for p in fast.points]
    assert minutes == sorted(minutes)

    slow = routes["90104"]
    assert all(p.is_stop for p in slow.points)


def test_route_points_table(loaded):
    stage_route_points(loaded)
    rows = loaded.execute(
        "SELECT station_code, point_seq, is_stop, progress FROM route_points "
        "WHERE train_id = 'central-90106' ORDER BY point_seq"
    ).fetchall()
    assert rows[0] == ("KYN", 0, True, 0.0)
    assert rows[-1] == ("CSMT", 25, True, 1.0)
    assert rows[1][:3] == ("THK", 1, False)


def test_sunday_schedule_days():
    assert is_sunday_schedule(date(2026, 10, 4))  # a Sunday
    assert is_sunday_schedule(date(2026, 10, 2))  # Gandhi Jayanti, a Friday
    assert is_holiday(date(2026, 8, 15)) and not is_holiday(date(2026, 8, 14))
    assert not is_sunday_schedule(date(2026, 10, 5))
