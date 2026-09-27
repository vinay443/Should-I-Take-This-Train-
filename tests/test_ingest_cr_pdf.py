"""Tests for the Central Railway PDF converter.

`fixtures/cr_pdf_words.json` holds the word positions of three real pages of the main
line PTT (w.e.f. 05.10.2024): pages 1 and 2 of the DOWN PDF and page 6 of the UP PDF,
extracted with `words_from_page`. Expected values were checked by hand against the
rendered PDF pages (and, for 96301 and 95324, against NTES).
"""

import io
import json
from datetime import datetime
from pathlib import Path

import pytest

from sitt.ingest import cr_pdf
from sitt.ingest.cr_pdf import (
    Column,
    ConversionError,
    PageGrid,
    SourceInfo,
    Word,
    grid_trains,
    parse_grid,
    station_for,
    write_csv,
)
from sitt.ingest.timetable import load_timetable, read_timetable
from sitt.timetable import next_trains

FIXTURE = Path(__file__).parent / "fixtures" / "cr_pdf_words.json"
MONDAY = datetime(2026, 9, 28)


@pytest.fixture(scope="module")
def grids() -> list[PageGrid]:
    pages = json.loads(FIXTURE.read_text(encoding="utf-8"))["pages"]
    return [parse_grid([Word(*w) for w in p["words"]], p["page"]) for p in pages]


@pytest.fixture(scope="module")
def trains(grids):
    conversion = grid_trains(grids)
    assert conversion.warnings == []
    return {t.number: t for t in conversion.trains}


def stops(train):
    return [(s.station.code, s.time) for s in train.stops]


def test_grid_shape(grids):
    down1, down2, up6 = grids
    assert [len(g.columns) for g in grids] == [25, 25, 16]
    assert down1.columns[0].number == "96301"
    assert down1.stations[0].code == "CSMT" and down1.stations[-1].code == "KSRA"
    assert up6.stations[0].code == "KSRA" and up6.stations[-1].code == "CSMT"
    assert len(down1.stations) == len(up6.stations) == 51


def test_all_stops_slow_train(trains):
    train = trains["96301"]  # A 1: first Ambernath slow, CSMT 00:02
    assert (train.direction, train.service_type, train.days) == ("down", "slow", "daily")
    assert train.service_code == "A 1"
    assert train.destination == "Ambernath"
    assert len(train.stops) == 29
    assert stops(train)[:2] == [("CSMT", "00:02"), ("MSD", "00:05")]
    # The leg beyond Kalyan matches NTES's schedule for 96301.
    assert stops(train)[-4:] == [
        ("KYN", "01:30"),
        ("VLDI", "01:35"),
        ("ULNR", "01:38"),
        ("ABH", "01:46"),
    ]


def test_fast_train_skips_stations(trains):
    train = trains["95001"]  # KP 1: Khopoli fast
    assert train.service_type == "fast"
    assert train.destination == "Khopoli"
    assert stops(train)[:8] == [
        ("CSMT", "04:35"),
        ("BY", "04:42"),
        ("DR", "04:48"),
        ("CLA", "04:57"),
        ("GC", "05:02"),
        ("TNA", "05:20"),
        ("DI", "05:36"),
        ("KYN", "05:53"),
    ]
    assert stops(train)[-1] == ("KHPI", "07:09")


def test_train_starting_mid_line(trains):
    train = trains["96103"]  # TS 1: Thane to Karjat, all stations
    assert stops(train)[0] == ("TNA", "05:00")
    assert stops(train)[-1] == ("KJT", "06:25")
    assert train.service_type == "slow"


def test_markers(trains):
    assert trains["97003"].days == "mon-sat"  # X
    assert trains["95701"].ac is True
    assert trains["95701"].days == "daily"
    assert (trains["95703"].cars, trains["95703"].days) == (15, "mon-sat")  # 15 C, X
    assert trains["96301"].cars is None and trains["96301"].ac is False
    ladies = trains["97032"]  # K 28, L SPL, X
    assert (ladies.notes, ladies.days, ladies.direction) == (["ladies_special"], "mon-sat", "up")


def test_up_trains(trains):
    train = trains["96614"]  # TL 16: Titwala to CSMT, all stations
    assert (train.direction, train.service_type, train.destination) == ("up", "slow", "CSMT")
    assert stops(train)[0] == ("TLA", "07:45")
    assert ("KYN", "08:01") in stops(train)
    assert stops(train)[-1] == ("CSMT", "09:30")
    assert trains["95404"].service_type == "fast"


def test_csv_loads_and_answers_next_trains(trains, con, tmp_path):
    out = io.StringIO()
    source = SourceInfo("SUB PTT UP ML'24.pdf", "abc123", "2024-10-05", "https://example/up.pdf")
    write_csv(out, list(trains.values()), [source])
    text = out.getvalue()
    assert "# source: SUB PTT UP ML'24.pdf, edition w.e.f. 2024-10-05" in text
    assert "#   sha256: abc123" in text

    csv_path = tmp_path / "central.csv"
    csv_path.write_text(text, encoding="utf-8")
    load_timetable(con, read_timetable(csv_path))

    # Kalyan departures on UP page 6, read off the rendered PDF page.
    trips = next_trains(con, "Kalyan", "CSMT", MONDAY.replace(hour=8), n=5)
    assert [(t.number, f"{t.departure:%H:%M}", f"{t.arrival:%H:%M}") for t in trips] == [
        ("96614", "08:01", "09:30"),
        ("97032", "08:09", "09:38"),
        ("95404", "08:14", "09:18"),
        ("95710", "08:14", "09:30"),
        ("95308", "08:23", "09:26"),
    ]


def test_cli_writes_csv_from_pdf(tmp_path, monkeypatch, grids):
    monkeypatch.setattr(cr_pdf, "read_pdf_grids", lambda path: grids)
    pdf = tmp_path / "up.pdf"
    pdf.write_bytes(b"not really a pdf")
    out = tmp_path / "out.csv"

    assert cr_pdf.main([str(pdf), "-o", str(out), "--edition", "2024-10-05"]) == 0

    text = out.read_text(encoding="utf-8")
    assert "# source: up.pdf, edition w.e.f. 2024-10-05" in text
    assert len(read_timetable(out).trains) == 66


def _grid(markers: list[str], cells: dict[str, str]) -> PageGrid:
    stations = [station_for(name) for name in ("CSMT", "Byculla", "Dadar", "Kurla")]
    column = Column("90001", page=1, markers=markers)
    column.cells = {station_for(name): value for name, value in cells.items()}
    return PageGrid(page=1, stations=stations, columns=[column])


ALL_STOPS = {"CSMT": "10:00", "Byculla": "10:07", "Dadar": "10:12", "Kurla": "10:20"}


def test_passing_a_station_makes_a_train_fast():
    slow = grid_trains([_grid(["A 1"], ALL_STOPS)]).trains[0]
    fast = grid_trains([_grid(["A 1"], {**ALL_STOPS, "Byculla": "…"})]).trains[0]
    assert (slow.service_type, fast.service_type) == ("slow", "fast")
    # A pass after the last stop doesn't count: the train just ends there.
    ends_early = grid_trains([_grid(["A 1"], {**ALL_STOPS, "Kurla": "..."})]).trains[0]
    assert ends_early.service_type == "slow"
    assert ends_early.destination == "Dadar"


def test_unknown_day_marker_rejects_the_train():
    conversion = grid_trains([_grid(["A 1", "S"], ALL_STOPS)])
    assert conversion.trains == []
    assert conversion.rejected == {"90001": "page 1: unknown marker 'S'"}
    assert conversion.warnings == ["rejected train 90001: page 1: unknown marker 'S'"]


def test_unreadable_cell_rejects_the_train():
    conversion = grid_trains([_grid(["A 1"], {**ALL_STOPS, "Dadar": "`"})])
    assert conversion.trains == []
    assert "unreadable cell '`' at Dadar" in conversion.rejected["90001"]


def test_times_out_of_order_reject_the_train():
    conversion = grid_trains([_grid(["A 1"], {**ALL_STOPS, "Dadar": "13:12"})])
    assert "doesn't follow" in conversion.rejected["90001"]


def test_duplicate_listing():
    same = grid_trains([_grid(["A 1"], ALL_STOPS), _grid(["A 1"], ALL_STOPS)])
    assert len(same.trains) == 1
    assert same.rejected == {}
    different = grid_trains([_grid(["A 1"], ALL_STOPS), _grid(["A 1", "X"], ALL_STOPS)])
    assert different.trains == []
    assert "different details" in different.rejected["90001"]


def test_unknown_station_label_fails_the_page():
    words = [
        Word("STATION", 50, 80, 100),
        Word("90001", 100, 118, 100),
        Word("90003", 126, 144, 100),
        Word("Atlantis", 50, 80, 120),
        Word("10:00", 101, 117, 120),
    ]
    with pytest.raises(ConversionError, match="unknown station 'Atlantis'"):
        parse_grid(words, page=1)


def test_known_sources_have_editions():
    for edition, url in cr_pdf.KNOWN_SOURCES.values():
        assert edition == "2024-10-05"
        assert url.startswith("https://cr.indianrailways.gov.in/")
