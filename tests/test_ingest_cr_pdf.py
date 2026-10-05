"""Tests for the Central Railway PDF converter.

`fixtures/cr_pdf_words.json` holds the word positions of three real pages of the main
line PTT (w.e.f. 05.10.2024): pages 1 and 2 of the DOWN PDF and page 6 of the UP PDF,
extracted with `words_from_page`. Expected values were checked by hand against the
rendered PDF pages (and, for 96301 and 95324, against NTES).
"""

import copy
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
    apply_supplement,
    column_markers,
    column_train,
    grid_direction,
    grid_trains,
    parse_grid,
    station_for,
    write_csv,
)
from sitt.ingest.timetable import load_timetable, read_timetable
from sitt.timetable import next_trains

FIXTURE = Path(__file__).parent / "fixtures" / "cr_pdf_words.json"
SUPPLEMENT_FIXTURE = Path(__file__).parent / "fixtures" / "cr_pdf_supplement_words.json"
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
    kinds = []
    for edition, url, kind in cr_pdf.KNOWN_SOURCES.values():
        assert edition in ("2024-10-05", "2025-04-16", "2026-08-15")
        assert url.startswith("https://cr.indianrailways.gov.in/")
        kinds.append(kind)
    assert sorted(kinds) == ["15car", "ac", "main", "main"]


# --- Supplements ---------------------------------------------------------------------
#
# `fixtures/cr_pdf_supplement_words.json` holds page 4 of the AC services PDF and page 2
# of the 15-car services PDF (both UP), extracted the same way as the main fixture.


@pytest.fixture(scope="module")
def supplement_grids() -> dict[str, PageGrid]:
    pages = json.loads(SUPPLEMENT_FIXTURE.read_text(encoding="utf-8"))["pages"]
    return {p["kind"]: parse_grid([Word(*w) for w in p["words"]], p["page"]) for p in pages}


def _column(grid: PageGrid, number: str) -> Column:
    return next(c for c in grid.columns if c.number == number)


def test_ac_supplement_page_is_read(supplement_grids):
    grid = supplement_grids["ac"]
    assert grid_direction(grid) == "up"
    assert len(grid.columns) == 13 and len(grid.stations) == 51  # footnote row skipped

    train = column_train(_column(grid, "95702"), grid.stations, "up")  # K 12
    assert (train.ac, train.days, train.service_type, train.notes) == (True, "daily", "fast", [])
    assert stops(train)[0] == ("KYN", "06:32") and stops(train)[-1] == ("CSMT", "07:39")

    # "AC#" is AC on weekdays only; this column also has the stray 00:00 at Kasara.
    markers = column_markers(_column(grid, "97026"))
    assert (markers.ac, markers.notes, markers.days) == (True, ["non_ac_weekends"], "mon-sat")
    assert column_train(_column(grid, "97026"), grid.stations, "up") == (
        "page 4: 07:34 at Kalyan doesn't follow 00:00 at Kasara"
    )


def test_15_car_supplement_page_is_read(supplement_grids):
    grid = supplement_grids["15car"]
    assert grid_direction(grid) == "up"
    assert len(grid.columns) == 27
    assert grid.stations[0].code == "KHPI" and grid.stations[-1].code == "CSMT"
    assert {"KOPR", "DLV"} <= {s.code for s in grid.stations}  # printed as KOPAR and DLY

    # "DR" in the CSMT row: the train ends at Dadar.
    dadar = column_train(_column(grid, "95002"), grid.stations, "up")
    assert (dadar.service_code, dadar.cars, dadar.days) == ("DKP 2", 15, "daily")
    assert stops(dadar)[0] == ("KHPI", "04:50") and stops(dadar)[-1] == ("DR", "07:01")
    assert ("DLV", "04:58") in stops(dadar)

    assert column_train(_column(grid, "95712"), grid.stations, "up").days == "mon-sat"  # X
    assert column_train(_column(grid, "95142"), grid.stations, "up") == (
        "page 2: unreadable cell 'TXL' at Chinchpokli"
    )


def test_15_car_supplement_over_the_main_edition(trains, supplement_grids):
    """UP page 6 of the main edition shares 95712 and 95310 with the supplement page."""
    current = {n: copy.deepcopy(t) for n, t in trains.items()}
    assert (current["95712"].cars, current["95310"].cars) == (15, None)

    result = apply_supplement(current, [supplement_grids["15car"]], "15car")

    assert (current["95712"].cars, current["95310"].cars) == (15, 15)
    assert current["95310"].stops == trains["95310"].stops  # same timings as in 2024
    assert "95310: marked 15-car" in result.lines
    assert not any(line.startswith("95712:") for line in result.lines)  # unchanged
    # Trains not on the main fixture's pages are added, except the unreadable one.
    assert "95002" in current and "95142" not in current
    assert (
        "95142: row not used (page 2: unreadable cell 'TXL' at Chinchpokli); "
        "not in the timetable, so not added"
    ) in result.lines
    assert (result.listed, result.unchanged, result.flagged, result.skipped) == (27, 1, 1, 1)
    assert result.added == 24


def _supplement(markers, cells, number="90001"):
    grid = _grid(markers, cells)
    grid.columns[0].number = number
    return grid


def _base(markers=("A 1",), cells=None) -> dict:
    return {t.number: t for t in grid_trains([_grid(list(markers), cells or ALL_STOPS)]).trains}


def test_supplement_replaces_a_changed_train_and_keeps_what_it_does_not_state():
    current = _base(("A 1", "15 C", "L SPL"))
    later = {**ALL_STOPS, "Byculla": "…", "Kurla": "10:19"}

    result = apply_supplement(current, [_supplement(["A 1", "AC#", "X"], later)], "ac")

    train = current["90001"]
    assert (train.ac, train.cars, train.days, train.service_type) == (True, 15, "mon-sat", "fast")
    assert train.notes == ["ladies_special", "non_ac_weekends"]
    assert stops(train)[-1] == ("CLA", "10:19")
    assert result.lines == [
        "90001: marked AC; days daily -> mon-sat; "
        "stops (CSMT 10:00 - Kurla 10:20, 4 stops) -> (CSMT 10:00 - Kurla 10:19, 3 stops); "
        "slow -> fast; non-AC at weekends"
    ]
    assert (result.flagged, result.changed, result.unchanged) == (1, 1, 0)


def test_supplement_adds_a_new_train_and_names_a_possible_predecessor():
    current = _base()
    result = apply_supplement(
        current, [_supplement(["A 1", "15 C"], ALL_STOPS, number="90003")], "15car"
    )
    assert sorted(current) == ["90001", "90003"]  # nothing is removed
    assert result.added == 1
    assert result.lines == [
        "90003: added, A 1 slow (CSMT 10:00 - Kurla 10:20, 4 stops), daily. "
        "Train 90001 has the same service code; check whether this replaces it"
    ]


def test_unreadable_supplement_row_only_sets_the_flag_when_it_agrees():
    # A stray 00:00 (as in the AC PDF's Kasara row) makes the row unreadable.
    stray = {"CSMT": "00:00", "Byculla": "10:07", "Dadar": "10:12", "Kurla": "10:20"}
    base_cells = {"Byculla": "10:07", "Dadar": "10:12", "Kurla": "10:20"}

    current = _base(cells=base_cells)
    result = apply_supplement(current, [_supplement(["A 1", "AC"], stray)], "ac")
    assert current["90001"].ac is True
    assert stops(current["90001"])[0] == ("BY", "10:07")  # timings untouched
    assert (result.flag_only, result.skipped) == (1, 0)
    assert result.lines[0].startswith("90001: marked AC; timings kept because the row can't be")

    # If the readable cells disagree with the train we have, nothing is applied.
    current = _base(cells={**base_cells, "Kurla": "10:25"})
    result = apply_supplement(current, [_supplement(["A 1", "AC"], stray)], "ac")
    assert current["90001"].ac is False
    assert (result.flag_only, result.skipped) == (0, 1)
    assert result.lines[0].endswith("left as is")

    # An unknown cell (like "TXL") with otherwise identical stops: flag only.
    current = _base(cells=base_cells)
    odd = {**base_cells, "CSMT": "TXL"}
    result = apply_supplement(current, [_supplement(["A 1", "15 C"], odd)], "15car")
    assert (current["90001"].cars, result.flag_only) == (15, 1)


def test_supplement_row_without_its_marker_is_not_used():
    current = _base()
    result = apply_supplement(current, [_supplement(["A 1"], ALL_STOPS)], "ac")
    assert current["90001"].ac is False
    assert result.lines == ["90001: row not used (page 1: no AC marker); left as is"]


def test_flag_missing_from_the_supplement_is_reported_not_removed():
    current = _base(("A 1", "AC"))
    result = apply_supplement(
        current, [_supplement(["K 1", "AC"], ALL_STOPS, number="90005")], "ac"
    )
    assert current["90001"].ac is True
    assert "90001: AC in the earlier edition but not in the supplement; left as is" in result.lines


def test_station_code_cell_needs_a_time_at_that_station():
    ends_at_dadar = {"CSMT": "DR", "Byculla": "10:07", "Dadar": "10:12", "Kurla": "10:20"}
    train = grid_trains([_grid(["A 1"], ends_at_dadar)]).trains[0]
    assert stops(train)[0] == ("BY", "10:07")
    no_dadar = {**ends_at_dadar, "Dadar": "…"}
    conversion = grid_trains([_grid(["A 1"], no_dadar)])
    assert conversion.rejected == {"90001": "page 1: cell 'DR' at CSMT, but no time at Dadar"}


def test_cli_with_supplements(tmp_path, monkeypatch, grids, supplement_grids, capsys):
    pages = {
        "up.pdf": grids,
        "ac.pdf": [supplement_grids["ac"]],
        "cars.pdf": [supplement_grids["15car"]],
    }
    monkeypatch.setattr(cr_pdf, "read_pdf_grids", lambda path: pages[Path(path).name])
    for name in pages:
        (tmp_path / name).write_bytes(name.encode())
    out = tmp_path / "out.csv"

    code = cr_pdf.main(
        [
            str(tmp_path / "up.pdf"),
            "-o",
            str(out),
            "--edition",
            "2024-10-05",
            "--ac-supplement",
            str(tmp_path / "ac.pdf"),
            "--15-car-supplement",
            str(tmp_path / "cars.pdf"),
        ]
    )

    assert code == 0
    printed = capsys.readouterr().out
    assert "AC supplement: 13 trains listed" in printed
    assert "15-car supplement: 27 trains listed" in printed
    text = out.read_text(encoding="utf-8")
    assert "# AC supplement: ac.pdf, edition w.e.f. unknown" in text
    assert "# 15-car supplement: cars.pdf, edition w.e.f. unknown" in text
    timetable = read_timetable(out)  # still loads, with the supplement's trains
    by_number = {t.number: t for t in timetable.trains}
    assert by_number["95702"].is_ac is True
    assert by_number["95310"].car_count == 15


def test_known_pdf_given_as_the_wrong_kind_is_refused(tmp_path, monkeypatch):
    pdf = tmp_path / "ac.pdf"
    pdf.write_bytes(b"x")
    digest = cr_pdf.hashlib.sha256(b"x").hexdigest()
    monkeypatch.setitem(cr_pdf.KNOWN_SOURCES, digest, ("2025-04-16", "https://x", "ac"))
    assert cr_pdf.source_info(pdf, kind="ac").edition == "2025-04-16"
    with pytest.raises(ConversionError, match="is the ac PDF, but it was given as the main PDF"):
        cr_pdf.source_info(pdf)
