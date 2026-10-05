"""Convert Central Railway's Mumbai suburban timetable PDFs to the timetable CSV format.

The output loads with sitt.ingest.timetable (format: docs/timetable-format.md).

    python -m sitt.ingest.cr_pdf dn.pdf up.pdf -o central.csv [--edition 2024-10-05]

Sources
-------
Central Railway publishes the Pocket/Public Time Table (PTT) as PDFs on
https://cr.indianrailways.gov.in/view_section.jsp?lang=0&id=0,5,2360
(Time Table -> Mumbai Suburban). This module handles the main line edition, one PDF
per direction:

- DOWN (CSMT -> Kalyan -> Kasara/Khopoli):
  https://cr.indianrailways.gov.in/cris//uploads/files/1728294831372-SUB%20PTT%20DN%20ML'24.pdf
- UP (Kasara/Khopoli -> Kalyan -> CSMT):
  https://cr.indianrailways.gov.in/cris//uploads/files/1728294891897-SUB%20PTT%20UP%20ML'24.pdf

Both are the edition w.e.f. 05.10.2024. The PDFs don't state their own edition date, so
it comes from `KNOWN_SOURCES` (matched by SHA-256) or from `--edition`. Download the
PDFs yourself and pass local paths; they are not kept in the repository.

Layout and how it's read
------------------------
Each page is a grid: one column per train, one row per station. The header row
("STATION"/"Stations") holds the 5-digit train numbers. Below it are marker rows: the
service code (e.g. "A 1" = first Ambernath local), then any of "AC", "15 C" (15-car
rake), "$" (ladies coaches), "L SPL" (ladies special) and the day codes. Then come the
station rows in route order. Beyond Kalyan the rows run branch by branch (Karjat, then
Khopoli, then Kasara in the down PDF), and no train runs on two branches.

Plain text extraction collapses empty cells and shifts times into the wrong columns,
so the grid is rebuilt from word positions: columns are centred on the train numbers
in the header row, rows are clustered by the words' y-position, and station names
are the words left of the first column. A cell holds a time ("05:07" or "5:07"),
"…" or "..." (the train passes the station without stopping), or nothing (the
train doesn't run on that section).

Rules
-----
- Stops: every cell with a time. A train's stops are in page-row order.
- Direction: a PDF whose first station row is CSMT is `down`; one whose last station
  row is CSMT is `up`.
- Fast/slow: a train is `fast` if it passes ("…") at least one station between its
  first and last stop, and `slow` if it calls at every station there. Semi-fast
  trains (fast to Thane, then all stations) therefore count as fast.
- Destination: the name of the train's last stop.
- Times: the PTT gives one time per station. It goes in `scheduled_departure` at the
  first stop, `scheduled_arrival` at the last stop and in both elsewhere.
- Running days: no day code means daily, "X" means not on Sunday/holiday (mon-sat)
  and "XX" means not on Saturday/Sunday/holiday (mon-fri). Holidays that run to the
  Sunday schedule can't be expressed in the CSV, so those trains are shown as running
  on holidays that fall on weekdays. The CSV header says so. A marker the converter
  doesn't know rejects that train with a warning rather than guessing what it means.

Trains that can't be converted cleanly (unknown markers, unreadable cells, times out
of order, fewer than two stops, the same number listed twice with different stops)
are left out and reported as warnings, so the rest of the file still loads.

Supplements
-----------
Central Railway amends the main edition with smaller PDFs on the same page, which
list only the affected services, in the same grid:

- AC services on the main line (w.e.f. 16.04.2025), `--ac-supplement`:
  https://cr.indianrailways.gov.in/cris//uploads/files/1770378640224-Suburban%20AC%20Services%20wef%2016.04.2025.pdf
- 15-car services on the main line (w.e.f. 15.08.2026), `--15-car-supplement`:
  https://cr.indianrailways.gov.in/cris//uploads/files/1786691264142-PTT%20OF%2015%20CAR%20TT%20ON%20MAINLINE%20WEF%2015%20AUG%2026.pdf

    python -m sitt.ingest.cr_pdf dn.pdf up.pdf -o central.csv \
        --ac-supplement ac.pdf --15-car-supplement cars.pdf

They are applied over the main edition in date order (`apply_supplement`):

- A train the supplement lists replaces the same-numbered train: its stops, running
  days and service code come from the supplement, which is newer. The AC supplement
  sets `ac`; the 15-car supplement sets `cars` to 15. Whatever the supplement doesn't
  state (the car count in the AC PDF, AC in the 15-car PDF, ladies' special) is kept.
- A train number that isn't in the timetable yet is added. Whether it replaced an
  older service isn't stated, so nothing is removed; the report names any existing
  train with the same service code so a person can check.
- "AC#" means the train runs without AC on Saturdays, Sundays and nominated holidays.
  It is stored as AC with the note `non_ac_weekends`.
- Every change is reported, one line per train.

Quirks of the supplement PDFs, each handled explicitly:

- The AC PDF has a footnote row under the grid, which is skipped by its wording.
- The 15-car PDF labels rows with station codes ("KOPAR" for Kopar, and "DLY" once
  for Dolavli), has filler rows labelled "0", and pads marker rows with "0"/"00:00".
- A station code in a cell (the 15-car PDF writes "DR" in the CSMT row) says where the
  train really starts or ends. It is accepted only if the train has a time there.
- A supplement row that can't be read (a stray "00:00" in the Kasara row of trains
  that start at Kalyan, or the one cell reading "TXL") is not used for timings. If
  every stop of the existing train is in the row with the same time, the flag (AC or
  15-car) is still applied and the 2024 timings are kept; otherwise the row is
  skipped. Either way it is reported.
"""

import argparse
import csv
import hashlib
import re
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from statistics import median
from typing import Literal

Direction = Literal["up", "down"]

SourceKind = Literal["main", "ac", "15car"]

# The main PDFs don't carry their edition date, so known files are recognised by hash.
KNOWN_SOURCES: dict[str, tuple[str, str, SourceKind]] = {
    # sha256: (edition date, download URL, kind)
    "b5d8d8648af1c828b35d20920dc72ffb70f911b85585b9db711fd01a52b63c95": (
        "2024-10-05",
        "https://cr.indianrailways.gov.in/cris//uploads/files/"
        "1728294831372-SUB%20PTT%20DN%20ML'24.pdf",
        "main",
    ),
    "68f5236a46352675c5f5067ac8c05614f982fa1c86b224eedb3af5e1c64ede2f": (
        "2024-10-05",
        "https://cr.indianrailways.gov.in/cris//uploads/files/"
        "1728294891897-SUB%20PTT%20UP%20ML'24.pdf",
        "main",
    ),
    "405440ab710ba8ceffe4f0ff6fc0dc2142c51da2921dbfc9c0058efe4e8f0074": (
        "2025-04-16",
        "https://cr.indianrailways.gov.in/cris//uploads/files/"
        "1770378640224-Suburban%20AC%20Services%20wef%2016.04.2025.pdf",
        "ac",
    ),
    "15e93c4a214063a0e95275a91b43db78d8ff1beb316b109ffaffbcd1d48cd81c": (
        "2026-08-15",
        "https://cr.indianrailways.gov.in/cris//uploads/files/"
        "1786691264142-PTT%20OF%2015%20CAR%20TT%20ON%20MAINLINE%20WEF%2015%20AUG%2026.pdf",
        "15car",
    ),
}


@dataclass(frozen=True)
class Station:
    code: str
    name: str


# Station labels as printed in the PDFs, with the spellings seen in either direction.
# Codes are checked against NTES's station list (2026-09-27) except KLY, DLV, LWJ and
# KHPI, which NTES doesn't list.
# Sion is SION, not SIN: NTES's station list
# (https://enquiry.indianrail.gov.in/mntes/javascripts/station_data.js, checked
# 2026-10-05) has {"code":"SION","name":"SION"} and no SIN entry. Wikipedia and some
# third-party sites still give the older code SIN, which the bot accepts as an alias.
_STATIONS: dict[Station, tuple[str, ...]] = {
    Station("CSMT", "CSMT"): ("CSMT",),
    Station("MSD", "Masjid"): ("Masjid",),
    Station("SNRD", "Sandhurst Road"): ("Sandhurst Road",),
    Station("BY", "Byculla"): ("Byculla",),
    Station("CHG", "Chinchpokli"): ("Chinchpokli",),
    Station("CRD", "Currey Road"): ("Currey Road",),
    Station("PR", "Parel"): ("Parel",),
    Station("DR", "Dadar"): ("Dadar",),
    Station("MTN", "Matunga"): ("Matunga",),
    Station("SION", "Sion"): ("Sion",),
    Station("CLA", "Kurla"): ("Kurla",),
    Station("VVH", "Vidyavihar"): ("Vidyavihar",),
    Station("GC", "Ghatkopar"): ("Ghatkopar",),
    Station("VK", "Vikhroli"): ("Vikhroli",),
    Station("KJRD", "Kanjur Marg"): ("Kanjur Marg",),
    Station("BND", "Bhandup"): ("Bhandup",),
    Station("NHU", "Nahur"): ("Nahur",),
    Station("MLND", "Mulund"): ("Mulund",),
    Station("TNA", "Thane"): ("Thane",),
    Station("KLVA", "Kalva"): ("Kalva",),
    Station("MBQ", "Mumbra"): ("Mumbra",),
    Station("DIVA", "Diva"): ("Diva", "Diwa"),
    Station("KOPR", "Kopar"): ("Kopar",),
    Station("DI", "Dombivli"): ("Dombivli",),
    Station("THK", "Thakurli"): ("Thakurli",),
    Station("KYN", "Kalyan"): ("Kalyan",),
    Station("VLDI", "Vithalwadi"): ("Vithalwadi",),
    Station("ULNR", "Ulhasnagar"): ("Ulhas Nagar",),
    Station("ABH", "Ambernath"): ("Ambernath",),
    Station("BUD", "Badlapur"): ("Badlapur",),
    Station("VGI", "Vangani"): ("Vangani",),
    Station("SHLU", "Shelu"): ("Shelu",),
    Station("NRL", "Neral"): ("Neral",),
    Station("BVS", "Bhivpuri Road"): ("Bhivpuri Road",),
    Station("KJT", "Karjat"): ("Karjat",),
    Station("PDI", "Palasdhari"): ("Palasdhari",),
    Station("KLY", "Kelavli"): ("Kelavli",),
    Station("DLV", "Dolavli"): ("Dolavli",),
    Station("LWJ", "Lowjee"): ("Lowjee",),
    Station("KHPI", "Khopoli"): ("Khopoli",),
    Station("SHAD", "Shahad"): ("Shahad",),
    Station("ABY", "Ambivli"): ("Ambivli",),
    Station("TLA", "Titwala"): ("Titwala",),
    Station("KDV", "Khadavli"): ("Khadavli",),
    Station("VSD", "Vasind"): ("Vasind",),
    Station("ASO", "Asangaon"): ("Asangaon",),
    Station("ATG", "Atgaon"): ("Atgaon",),
    Station("THS", "Thansit"): ("Thansit",),
    Station("KE", "Khardi"): ("Khardi",),
    Station("OMB", "Umbermali"): ("Umbermali", "Umbermalli"),
    Station("KSRA", "Kasara"): ("Kasara",),
}


def _label_key(label: str) -> str:
    return re.sub(r"[^a-z]", "", label.lower())


_STATION_BY_LABEL: dict[str, Station] = {
    _label_key(label): station for station, labels in _STATIONS.items() for label in labels
}
_STATION_BY_CODE: dict[str, Station] = {station.code: station for station in _STATIONS}
# Row labels in the 15-car supplement that are neither a station's name nor its code.
# "DLY" is a typo for Dolavli (DLV): it sits between Lowjee and Kelavli on the UP page,
# and its times match Dolavli's in the main edition (e.g. 95002 at 04:58).
_CODE_LABEL_ALIASES: dict[str, str] = {"KOPAR": "KOPR", "DLY": "DLV"}
# Labels of rows that hold markers, not stations (15-car supplement).
_MARKER_ROW_LABELS = frozenset({"0", "code"})
# Rows of running text under the grid, recognised by their wording.
_FOOTNOTE_RES = (re.compile(r"Services marked as AC# means services running on"),)

# Header markers. Day codes restrict running days; flags describe the train.
DAY_CODES: dict[str, str] = {"X": "mon-sat", "XX": "mon-fri"}
_MARKER_RE = re.compile(r"(?P<cars>\d+)\s*C\b|(?P<lspl>L\s*SPL)\b|(?P<token>\S+)")
_FLAG_TOKENS = {"AC": "ac", "$": "ladies_coaches"}
# "AC#": runs without AC on Saturdays, Sundays and nominated holidays (AC supplement footnote).
NON_AC_WEEKENDS = "non_ac_weekends"
# Cells the supplements' spreadsheets pad marker rows with.
_FILLER_MARKERS = frozenset({"0", "00:00"})
_SERVICE_CODE_RE = re.compile(r"[A-Z]{1,4} ?\d{1,3}")

_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
PASS_MARKS = frozenset({"…", "...", "…."})
_HEADER_LABELS = frozenset({"station", "stations"})
_TRAIN_NUMBER_RE = re.compile(r"\d{5}")

ROW_TOLERANCE = 2.0  # points: words this close vertically are on the same row
MAX_STEP_MINUTES = 120  # same limits as the loader, so rejected trains are ours to report
MAX_RUN_MINUTES = 12 * 60


class ConversionError(ValueError):
    """The PDF doesn't look like a timetable this converter understands."""


@dataclass(frozen=True)
class Word:
    """A word on a page with its bounding box, in PDF points from the top-left."""

    text: str
    x0: float
    x1: float
    top: float

    @property
    def centre(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Column:
    """One train's column on one page, as read from the grid."""

    number: str
    page: int
    markers: list[str] = field(default_factory=list)  # marker rows, top to bottom
    cells: dict[Station, str] = field(default_factory=dict)  # station -> raw cell text


@dataclass
class PageGrid:
    page: int
    stations: list[Station]  # station rows, top to bottom
    columns: list[Column]


@dataclass(frozen=True)
class StopTime:
    station: Station
    time: str  # HH:MM


@dataclass
class ConvertedTrain:
    number: str
    direction: Direction
    service_type: str  # "fast" or "slow"
    days: str  # CSV running-days value, e.g. "daily", "mon-sat"
    stops: list[StopTime]
    page: int
    service_code: str = ""
    ac: bool = False
    cars: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def destination(self) -> str:
        return self.stops[-1].station.name


@dataclass
class Conversion:
    trains: list[ConvertedTrain]
    warnings: list[str]
    rejected: dict[str, str]  # train number -> reason
    supplements: list["SupplementResult"] = field(default_factory=list)


def station_for(label: str) -> Station | None:
    """The station a row label names: a name as printed, or a station code."""
    label = label.strip()
    code = _CODE_LABEL_ALIASES.get(label, label)
    return _STATION_BY_CODE.get(code) or _STATION_BY_LABEL.get(_label_key(label))


def words_from_page(page) -> list[Word]:
    """Words of a pdfplumber page. Exact duplicates (text drawn twice) are dropped."""
    seen = set()
    words = []
    for w in page.extract_words():
        key = (w["text"], round(w["x0"], 1), round(w["top"], 1))
        if key not in seen:
            seen.add(key)
            words.append(Word(w["text"], w["x0"], w["x1"], w["top"]))
    return words


def _rows(words: Iterable[Word]) -> list[list[Word]]:
    """Cluster words into rows by their top edge, then sort each row left to right."""
    rows: list[list[Word]] = []
    for word in sorted(words, key=lambda w: w.top):
        if rows and word.top - rows[-1][0].top <= ROW_TOLERANCE:
            rows[-1].append(word)
        else:
            rows.append([word])
    return [sorted(row, key=lambda w: w.x0) for row in rows]


def parse_grid(words: Sequence[Word], page: int) -> PageGrid:
    """Rebuild one page's timetable grid from its words."""
    rows = _rows(words)
    header_index = next(
        (
            i
            for i, row in enumerate(rows)
            if any(w.text.lower() in _HEADER_LABELS for w in row)
            and sum(bool(_TRAIN_NUMBER_RE.fullmatch(w.text)) for w in row) >= 2
        ),
        None,
    )
    if header_index is None:
        raise ConversionError(f"page {page}: no header row with train numbers")

    numbers = [w for w in rows[header_index] if _TRAIN_NUMBER_RE.fullmatch(w.text)]
    centres = [w.centre for w in numbers]
    width = median(b - a for a, b in zip(centres, centres[1:], strict=False))
    label_right = centres[0] - width / 2
    columns = [Column(w.text, page) for w in numbers]

    def column_of(word: Word) -> int | None:
        i = min(range(len(centres)), key=lambda i: abs(centres[i] - word.centre))
        return i if abs(centres[i] - word.centre) <= width / 2 else None

    stations: list[Station] = []
    for row in rows[header_index + 1 :]:
        text = " ".join(w.text for w in row)
        if any(footnote.match(text) for footnote in _FOOTNOTE_RES):
            continue
        label = " ".join(w.text for w in row if w.x1 <= label_right)
        if label.lower() in _MARKER_ROW_LABELS:
            label = ""
        cells: dict[int, list[str]] = defaultdict(list)
        for word in row:
            if word.x1 <= label_right:
                continue
            i = column_of(word)
            if i is None:
                raise ConversionError(
                    f"page {page}: {word.text!r} at x={word.x0:.0f} is outside every column"
                )
            cells[i].append(word.text)

        if not label:
            if stations:
                raise ConversionError(f"page {page}: row without a station name among stations")
            for i, texts in cells.items():
                if marker := " ".join(t for t in texts if t not in _FILLER_MARKERS):
                    columns[i].markers.append(marker)
            continue
        station = station_for(label)
        if station is None:
            raise ConversionError(f"page {page}: unknown station {label!r}")
        if station in stations:
            raise ConversionError(f"page {page}: station {label!r} listed twice")
        stations.append(station)
        for i, texts in cells.items():
            columns[i].cells[station] = " ".join(texts)

    if not stations:
        raise ConversionError(f"page {page}: no station rows")
    return PageGrid(page, stations, columns)


def read_pdf_grids(path: str | Path) -> list[PageGrid]:
    import pdfplumber  # imported here so the grid logic can be tested without PDFs

    with pdfplumber.open(path) as pdf:
        return [parse_grid(words_from_page(p), i) for i, p in enumerate(pdf.pages, start=1)]


def grid_direction(grid: PageGrid) -> Direction:
    if grid.stations[0].code == "CSMT":
        return "down"
    if grid.stations[-1].code == "CSMT":
        return "up"
    raise ConversionError(f"page {grid.page}: can't tell direction, CSMT is not at either end")


def _classify_cell(text: str) -> str | None:
    """'HH:MM' for a time, 'pass' for a pass mark, '' for empty, None if unreadable."""
    if not text:
        return ""
    if text in PASS_MARKS:
        return "pass"
    match = _TIME_RE.fullmatch(text)
    if match and int(match[1]) <= 23 and int(match[2]) <= 59:
        return f"{int(match[1]):02d}:{match[2]}"
    return None


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


@dataclass
class Markers:
    """What a column's marker rows say about the train."""

    service_code: str
    days: str = "daily"
    ac: bool = False
    cars: int | None = None
    notes: list[str] = field(default_factory=list)


def column_markers(column: Column) -> Markers | str:
    """Read a column's marker rows. Returns the Markers, or a rejection reason."""
    service_code = column.markers[0] if column.markers else ""
    if not _SERVICE_CODE_RE.fullmatch(service_code):
        return f"page {column.page}: unexpected service code {service_code!r}"
    markers = Markers(service_code)
    for marker in column.markers[1:]:
        # "AC #" and "AC#" are the same marker, printed in one cell or two.
        for match in _MARKER_RE.finditer(re.sub(r"\bAC\s*#", "AC#", marker)):
            token = match["token"]
            if match["cars"]:
                markers.cars = int(match["cars"])
            elif match["lspl"]:
                markers.notes.append("ladies_special")
            elif token in DAY_CODES:
                if markers.days != "daily":
                    return f"page {column.page}: more than one day code"
                markers.days = DAY_CODES[token]
            elif token == "AC#":
                markers.ac = True
                markers.notes.append(NON_AC_WEEKENDS)
            elif token in _FLAG_TOKENS:
                flag = _FLAG_TOKENS[token]
                if flag == "ac":
                    markers.ac = True
                else:
                    markers.notes.append(flag)
            else:
                return f"page {column.page}: unknown marker {token!r}"
    return markers


def column_times(column: Column, stations: Sequence[Station]) -> list[StopTime]:
    """Every cell of the column that holds a time, in row order, whatever else is wrong."""
    times = []
    for station in stations:
        kind = _classify_cell(column.cells.get(station, ""))
        if kind and kind != "pass":
            times.append(StopTime(station, kind))
    return times


def column_train(column: Column, stations: Sequence[Station], direction: Direction):
    """Convert one column to a train. Returns a ConvertedTrain, or a rejection reason."""
    markers = column_markers(column)
    if isinstance(markers, str):
        return markers

    stops = []
    passes_between = False
    passed_since_last_stop = False
    for station in stations:
        text = column.cells.get(station, "")
        kind = _classify_cell(text)
        if kind is None and (named := _STATION_BY_CODE.get(text)) is not None:
            # A station code in a cell says the train starts or ends there instead
            # ("DR" in the CSMT row). Believe it only if the train has a time there.
            if _classify_cell(column.cells.get(named, "")) in (None, "", "pass"):
                return (
                    f"page {column.page}: cell {text!r} at {station.name}, "
                    f"but no time at {named.name}"
                )
            kind = ""
        if kind is None:
            return f"page {column.page}: unreadable cell {text!r} at {station.name}"
        if kind == "pass":
            passed_since_last_stop = True
        elif kind:
            if stops and passed_since_last_stop:
                passes_between = True
            passed_since_last_stop = False
            stops.append(StopTime(station, kind))

    if len(stops) < 2:
        return f"page {column.page}: fewer than two stops"
    run = 0
    for prev, cur in zip(stops, stops[1:], strict=False):
        step = (_minutes(cur.time) - _minutes(prev.time)) % (24 * 60)
        if step > MAX_STEP_MINUTES:
            return (
                f"page {column.page}: {cur.time} at {cur.station.name} doesn't follow "
                f"{prev.time} at {prev.station.name}"
            )
        run += step
    if run >= MAX_RUN_MINUTES:
        return f"page {column.page}: runs for 12 hours or more"

    return ConvertedTrain(
        number=column.number,
        direction=direction,
        service_type="fast" if passes_between else "slow",
        days=markers.days,
        stops=stops,
        page=column.page,
        service_code=markers.service_code,
        ac=markers.ac,
        cars=markers.cars,
        notes=markers.notes,
    )


def grid_trains(grids: Iterable[PageGrid]) -> Conversion:
    """Convert pages of one or more PDFs, rejecting trains that can't be read cleanly."""
    converted: dict[str, ConvertedTrain] = {}
    rejected: dict[str, str] = {}
    warnings: list[str] = []
    for grid in grids:
        direction = grid_direction(grid)
        for column in grid.columns:
            result = column_train(column, grid.stations, direction)
            number = column.number
            if isinstance(result, str):
                rejected[number] = result
                converted.pop(number, None)
                continue
            if number in rejected:
                continue
            earlier = converted.get(number)
            if earlier is None:
                converted[number] = result
            elif _same_service(earlier, result):
                warnings.append(f"train {number} listed on pages {earlier.page} and {result.page}")
            else:
                rejected[number] = (
                    f"listed on pages {earlier.page} and {result.page} with different details"
                )
                del converted[number]
    warnings.extend(f"rejected train {n}: {reason}" for n, reason in rejected.items())
    return Conversion(list(converted.values()), warnings, rejected)


def _same_service(a: ConvertedTrain, b: ConvertedTrain) -> bool:
    return (a.stops, a.days, a.direction, a.service_code) == (
        b.stops,
        b.days,
        b.direction,
        b.service_code,
    )


SUPPLEMENT_NAMES: dict[str, str] = {"ac": "AC supplement", "15car": "15-car supplement"}


@dataclass
class SupplementResult:
    """What applying one supplement did. `lines` has one entry per train touched."""

    kind: str
    listed: int = 0
    unchanged: int = 0  # already had the flag and the same details
    flagged: int = 0  # gained the flag (AC or 15-car), with or without other changes
    changed: int = 0  # stops, days, service code or fast/slow differ from before
    added: int = 0
    flag_only: int = 0  # row unreadable; flag applied, earlier timings kept
    skipped: int = 0  # row unreadable and not usable at all
    lines: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{SUPPLEMENT_NAMES[self.kind]}: {self.listed} trains listed; "
            f"{self.flagged} newly marked, {self.changed} with changed details, "
            f"{self.added} added, {self.unchanged} unchanged, "
            f"{self.flag_only} marked but timings kept, {self.skipped} skipped."
        )


def _has_flag(train: ConvertedTrain, kind: str) -> bool:
    return train.ac if kind == "ac" else train.cars == 15


def _span(train: ConvertedTrain) -> str:
    first, last = train.stops[0], train.stops[-1]
    return (
        f"{first.station.name} {first.time} - {last.station.name} {last.time}, "
        f"{len(train.stops)} stops"
    )


def _describe_changes(old: ConvertedTrain, new: ConvertedTrain) -> list[str]:
    changes = []
    if old.service_code != new.service_code:
        changes.append(f"service code {old.service_code} -> {new.service_code}")
    if old.days != new.days:
        changes.append(f"days {old.days} -> {new.days}")
    if old.stops != new.stops:
        changes.append(f"stops ({_span(old)}) -> ({_span(new)})")
    if old.service_type != new.service_type:
        changes.append(f"{old.service_type} -> {new.service_type}")
    if (NON_AC_WEEKENDS in old.notes) != (NON_AC_WEEKENDS in new.notes):
        changes.append(
            "non-AC at weekends" if NON_AC_WEEKENDS in new.notes else "AC every day it runs"
        )
    return changes


def apply_supplement(
    trains: dict[str, ConvertedTrain], grids: Iterable[PageGrid], kind: str
) -> SupplementResult:
    """Apply an AC ("ac") or 15-car ("15car") supplement to `trains`, in place.

    See "Supplements" in the module docstring for the rules. Nothing is guessed: a row
    that can't be read leaves the train's timings alone and is reported.
    """
    result = SupplementResult(kind)
    flag = "AC" if kind == "ac" else "15-car"
    seen: set[str] = set()
    for grid in grids:
        direction = grid_direction(grid)
        for column in grid.columns:
            number = column.number
            result.listed += 1
            if number in seen:
                result.lines.append(f"{number}: listed twice in the supplement; first kept")
                continue
            seen.add(number)
            current = trains.get(number)
            markers = column_markers(column)
            new = column_train(column, grid.stations, direction)
            problem = new if isinstance(new, str) else None
            marked = not isinstance(markers, str) and (
                markers.ac if kind == "ac" else markers.cars == 15
            )
            if not isinstance(markers, str) and not marked:
                # Listed in the supplement without its marker: don't assume it applies.
                problem = f"page {column.page}: no {flag} marker"
            if not marked or (problem and current is None):
                result.skipped += 1
                where = "not in the timetable, so not added" if current is None else "left as is"
                result.lines.append(f"{number}: row not used ({problem}); {where}")
                continue

            if problem:
                # Unreadable row for a train we have. The flag is safe to apply only if
                # the row agrees with the train's stops and its other times are filler.
                times = column_times(column, grid.stations)
                extra = [t for t in times if t not in current.stops]
                agrees = all(stop in times for stop in current.stops)
                if not agrees or any(t.time != "00:00" for t in extra):
                    result.skipped += 1
                    result.lines.append(f"{number}: row not used ({problem}); left as is")
                    continue
                gained = not _has_flag(current, kind)
                if kind == "ac":
                    current.ac = True
                    current.notes = [n for n in current.notes if n != NON_AC_WEEKENDS] + [
                        n for n in markers.notes if n == NON_AC_WEEKENDS
                    ]
                else:
                    current.cars = 15
                result.flag_only += 1
                result.lines.append(
                    f"{number}: {'marked ' + flag if gained else 'already ' + flag}; "
                    f"timings kept because the row can't be read ({problem})"
                )
                continue

            if current is None:
                trains[number] = new
                result.added += 1
                line = (
                    f"{number}: added, {new.service_code} {new.service_type} "
                    f"({_span(new)}), {new.days}"
                )
                same_code = sorted(
                    t.number
                    for t in trains.values()
                    if t.number != number
                    and t.service_code == new.service_code
                    and t.direction == new.direction
                )
                if same_code:
                    line += (
                        f". Train {', '.join(same_code)} has the same service code; "
                        "check whether this replaces it"
                    )
                result.lines.append(line)
                continue

            # The supplement is newer, so its row replaces the train. Keep what it
            # doesn't state.
            if kind == "ac":
                new.cars = current.cars
            else:
                new.ac = current.ac
            kept = [n for n in current.notes if n not in new.notes]
            if kind == "ac":
                kept = [n for n in kept if n != NON_AC_WEEKENDS]
            new.notes = kept + new.notes
            changes = _describe_changes(current, new)
            gained = not _has_flag(current, kind)
            trains[number] = new
            if gained:
                result.flagged += 1
            if changes:
                result.changed += 1
            if not gained and not changes:
                result.unchanged += 1
            else:
                what = ([f"marked {flag}"] if gained else []) + changes
                result.lines.append(f"{number}: {'; '.join(what)}")

    for train in trains.values():
        if _has_flag(train, kind) and train.number not in seen:
            result.lines.append(
                f"{train.number}: {flag} in the earlier edition but not in the supplement; "
                "left as is"
            )
    return result


CSV_COLUMNS = (
    "train_number",
    "destination",
    "service_type",
    "direction",
    "station_code",
    "station_name",
    "scheduled_arrival",
    "scheduled_departure",
    "days",
    # Extra columns, ignored by the loader but kept for later use.
    "service_code",
    "ac",
    "cars",
    "notes",
)


def csv_rows(train: ConvertedTrain) -> Iterable[list[str]]:
    last = len(train.stops) - 1
    for i, stop in enumerate(train.stops):
        yield [
            train.number,
            train.destination,
            train.service_type,
            train.direction,
            stop.station.code,
            stop.station.name,
            "" if i == 0 else stop.time,
            "" if i == last else stop.time,
            train.days,
            train.service_code,
            "yes" if train.ac else "no",
            "" if train.cars is None else str(train.cars),
            "|".join(train.notes),
        ]


@dataclass(frozen=True)
class SourceInfo:
    file_name: str
    sha256: str
    edition: str | None
    url: str | None
    kind: str = "main"


def source_info(path: str | Path, edition: str | None = None, kind: str = "main") -> SourceInfo:
    """Describe a source PDF. Raises ConversionError if a known file is passed as the wrong kind."""
    path = Path(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    known_edition, url, known_kind = KNOWN_SOURCES.get(digest, (None, None, None))
    if known_kind is not None and known_kind != kind:
        raise ConversionError(
            f"{path.name} is the {known_kind} PDF, but it was given as the {kind} PDF"
        )
    return SourceInfo(path.name, digest, edition or known_edition, url, kind)


_SOURCE_LABELS = {"main": "source", "ac": "AC supplement", "15car": "15-car supplement"}


def write_csv(out, trains: Sequence[ConvertedTrain], sources: Sequence[SourceInfo]) -> None:
    out.write("# Central Railway Mumbai suburban timetable, main line.\n")
    out.write(f"# Converted by sitt.ingest.cr_pdf on {date.today().isoformat()}.\n")
    for source in sources:
        out.write(
            f"# {_SOURCE_LABELS[source.kind]}: {source.file_name}, "
            f"edition w.e.f. {source.edition or 'unknown'}\n"
        )
        if source.url:
            out.write(f"#   url: {source.url}\n")
        out.write(f"#   sha256: {source.sha256}\n")
    out.write(
        "# Days: X = mon-sat, XX = mon-fri. Holidays that follow the Sunday schedule are\n"
        "# not represented, so those trains are listed as running on weekday holidays.\n"
    )
    if any(NON_AC_WEEKENDS in train.notes for train in trains):
        out.write(
            f"# Note {NON_AC_WEEKENDS}: an AC train that runs without AC on Saturdays,\n"
            "# Sundays and nominated holidays.\n"
        )
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for train in sorted(trains, key=lambda t: (t.direction, t.stops[0].time, t.number)):
        writer.writerows(csv_rows(train))


def convert(
    paths: Sequence[str | Path],
    edition: str | None = None,
    *,
    ac_supplement: str | Path | None = None,
    cars_supplement: str | Path | None = None,
):
    """Read the main PDFs, apply any supplements, and return (Conversion, [SourceInfo])."""
    sources = [source_info(p, edition) for p in paths]
    conversion = grid_trains(grid for p in paths for grid in read_pdf_grids(p))
    trains = {train.number: train for train in conversion.trains}
    # In date order: the AC supplement is from 2025, the 15-car one from 2026.
    for path, kind in ((ac_supplement, "ac"), (cars_supplement, "15car")):
        if path is None:
            continue
        sources.append(source_info(path, kind=kind))
        conversion.supplements.append(apply_supplement(trains, read_pdf_grids(path), kind))
    conversion.trains = list(trains.values())
    return conversion, sources


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sitt.ingest.cr_pdf",
        description="Convert Central Railway suburban timetable PDFs to a timetable CSV.",
    )
    parser.add_argument("pdf", nargs="+", type=Path, help="main line PTT PDF(s), UP and/or DN")
    parser.add_argument("-o", "--output", type=Path, required=True, help="CSV file to write")
    parser.add_argument(
        "--edition", help="edition date (w.e.f.) to record, if a main PDF isn't a known one"
    )
    parser.add_argument(
        "--ac-supplement", type=Path, help="AC services PDF to apply over the main edition"
    )
    parser.add_argument(
        "--15-car-supplement",
        dest="cars_supplement",
        type=Path,
        help="15-car services PDF to apply over the main edition (after the AC one)",
    )
    args = parser.parse_args(argv)

    try:
        conversion, sources = convert(
            args.pdf,
            args.edition,
            ac_supplement=args.ac_supplement,
            cars_supplement=args.cars_supplement,
        )
    except (ConversionError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for source in sources:
        if source.edition is None:
            hint = "; pass --edition" if source.kind == "main" else ""
            print(
                f"warning: {source.file_name} is not a known edition{hint}",
                file=sys.stderr,
            )
    with args.output.open("w", encoding="utf-8", newline="") as out:
        write_csv(out, conversion.trains, sources)

    for warning in conversion.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for supplement in conversion.supplements:
        print(supplement.summary())
        for line in supplement.lines:
            print(f"  {line}")
    stops = sum(len(t.stops) for t in conversion.trains)
    print(
        f"Wrote {len(conversion.trains)} trains ({stops} stops) to {args.output}; "
        f"rejected {len(conversion.rejected)}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
