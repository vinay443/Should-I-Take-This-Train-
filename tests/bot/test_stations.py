from collections import Counter

from sitt.bot.stations import STATIONS, lookup_keys, lookup_station


def test_lookup_keys_are_unambiguous():
    counts = Counter(key for station in STATIONS for key in set(lookup_keys(station)))
    assert [key for key, n in counts.items() if n > 1] == []


def test_line_runs_kalyan_to_csmt():
    assert STATIONS[0].code == "KYN"
    assert STATIONS[-1].code == "CSMT"


def test_lookup():
    assert lookup_station(" tna ").code == "TNA"
    assert lookup_station("kanjurmarg").code == "KJRD"
    assert lookup_station("nowhere") is None


def test_sion_uses_the_timetable_code_and_keeps_the_old_one():
    assert lookup_station("sion").code == "SION"
    assert lookup_station("SIN").code == "SION"
