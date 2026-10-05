from collections import Counter

import pytest
from conftest import SAMPLE_CSV

from sitt.bot.stations import (
    ALIASES,
    FALLBACK_DIRECTORY,
    FALLBACK_STATIONS,
    Station,
    StationDirectory,
    _key,
    directory_from,
    load_directory,
)
from sitt.db import init_db
from sitt.ingest.cr_pdf import _STATIONS as PDF_STATIONS
from sitt.ingest.timetable import load_timetable, read_timetable

# The whole Central main line, as the PDF converter names it.
FULL_LINE = StationDirectory([Station(s.code, s.name) for s in PDF_STATIONS])


def test_fallback_runs_csmt_to_kalyan_with_timetable_spellings():
    assert FALLBACK_STATIONS[0].code == "CSMT"
    assert FALLBACK_STATIONS[-1].code == "KYN"
    pdf_names = {s.code: s.name for s in PDF_STATIONS}
    assert all(pdf_names[s.code] == s.name for s in FALLBACK_STATIONS)


def test_lookup_by_code_name_and_alias():
    lookup = FALLBACK_DIRECTORY.lookup
    assert lookup(" tna ").code == "TNA"
    assert lookup("Thane").code == "TNA"
    assert lookup("kanjurmarg").code == "KJRD"
    assert lookup("Kanjur Marg").code == "KJRD"
    assert lookup("nowhere") is None
    assert lookup("") is None


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("vt", "CSMT"),
        ("CST", "CSMT"),
        ("dombivali", "DI"),
        ("kanjur", "KJRD"),
        ("sin", "SION"),
        ("sion", "SION"),
        ("Kalwa", "KLVA"),
        ("kalva", "KLVA"),
        ("diwa", "DIVA"),
        ("currey", "CRD"),
        ("sandhurst", "SNRD"),
    ],
)
def test_aliases(text, code):
    assert FALLBACK_DIRECTORY.lookup(text).code == code
    assert FULL_LINE.lookup(text).code == code


def test_aliases_beyond_kalyan_need_the_timetable():
    assert FALLBACK_DIRECTORY.lookup("ambarnath") is None  # not in the fallback stretch
    assert FULL_LINE.lookup("ambarnath").code == "ABH"
    assert FULL_LINE.lookup("Ulhas Nagar").code == "ULNR"
    assert FULL_LINE.lookup("titvala").code == "TLA"
    assert FULL_LINE.lookup("KSRA").name == "Kasara"


def test_aliases_point_at_real_stations_and_shadow_nothing():
    codes = {s.code for s in PDF_STATIONS}
    assert set(ALIASES.values()) <= codes
    assert all(alias == _key(alias) for alias in ALIASES)
    # An alias must not hide a different station's code or name.
    for station in PDF_STATIONS:
        for key in (_key(station.code), _key(station.name)):
            assert ALIASES.get(key, station.code) == station.code


def test_codes_and_names_are_unambiguous_on_the_full_line():
    keys = Counter(key for s in PDF_STATIONS for key in {_key(s.code), _key(s.name)})
    assert [key for key, n in keys.items() if n > 1] == []


def test_label():
    assert FULL_LINE.label("KYN") == "Kalyan (KYN)"
    assert FULL_LINE.label("CSMT") == "CSMT"
    assert FULL_LINE.label("XYZ") == "XYZ"


def test_directory_comes_from_the_stations_table(tmp_path):
    path = tmp_path / "sitt.duckdb"
    with init_db(path) as con:
        assert directory_from(con) is FALLBACK_DIRECTORY  # empty table
        load_timetable(con, read_timetable(SAMPLE_CSV))
    directory = load_directory(path)
    assert directory is not FALLBACK_DIRECTORY
    assert [s.code for s in directory.stations][:3] == ["CSMT", "MSD", "SNRD"]  # by seq
    # The sample timetable spells out CSMT's name; aliases still reach it.
    assert directory.lookup("vt").name == "Chhatrapati Shivaji Maharaj Terminus"
    assert directory.lookup("chhatrapati shivaji maharaj terminus").code == "CSMT"
