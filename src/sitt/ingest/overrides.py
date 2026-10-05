"""Manual corrections to the converted timetable, from a TOML file.

The PDF converter (sitt.ingest.cr_pdf) rejects what it can't read and never guesses. This
is where a person puts what the PDF really says: a train the converter rejected, a wrong
time, a missing marker. The file is `timetable_overrides.toml` beside this module, and it
is applied last, after the main PDFs and the supplements.

    [[train]]
    number = "96415"
    reason = "PTT DN page 7 prints 14:91 at Diva; the UP page and NTES both give 14:19."
    stop_times = { DIVA = "14:19" }

Every entry needs a `number` and a `reason`. What it does is set by `action`:

    action = "set"     (the default) change a train that exists. Any of:
                         days, service_type, service_code, ac, cars, notes
                         stops         the whole list, replacing the train's stops
                         stop_times    {CODE = "HH:MM"}: change a stop's time, or add a stop
                         remove_stops  [CODE, ...]: drop stops
    action = "add"     a train the converter doesn't have. Needs direction, service_type
                       and stops; days defaults to "daily".
    action = "remove"  drop a train.

The whole file is checked before anything is applied, and every problem is reported at
once with the entry it is in. An entry that changes nothing (the PDF has since been
fixed, say) is reported as such rather than failing. The full format, with examples, is
in docs/timetable-format.md.
"""

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

from sitt.ingest.cr_pdf import (
    _STATION_BY_CODE,
    _STATIONS,
    _TIME_RE,
    ConversionError,
    ConvertedTrain,
    StopTime,
    check_stops,
)
from sitt.ingest.timetable import parse_days

DEFAULT_FILE = "timetable_overrides.toml"
ACTIONS = ("set", "add", "remove")
_TRAIN_FIELDS = ("days", "service_type", "service_code", "ac", "cars", "notes")
_KEYS = frozenset(
    {"number", "reason", "action", "direction", "stops", "stop_times", "remove_stops"}
    | set(_TRAIN_FIELDS)
)
# Stations in line order from CSMT, branch by branch, as the PDFs list them.
_ORDER = {station.code: index for index, station in enumerate(_STATIONS)}


class OverrideError(ConversionError):
    """The overrides file is wrong. The message lists every problem found."""


@dataclass
class Override:
    index: int  # position in the file, from 1, for error messages
    number: str
    reason: str
    action: str = "set"
    direction: str | None = None
    fields: dict = field(default_factory=dict)  # of _TRAIN_FIELDS, as given
    stops: list[StopTime] | None = None
    stop_times: dict[str, str] = field(default_factory=dict)
    remove_stops: list[str] = field(default_factory=list)

    @property
    def where(self) -> str:
        return f"entry {self.index} (train {self.number})"


def default_path() -> Path:
    return Path(str(files("sitt.ingest").joinpath(DEFAULT_FILE)))


def _time(value) -> str | None:
    """'HH:MM' normalised, or None if `value` isn't a time."""
    match = _TIME_RE.fullmatch(value) if isinstance(value, str) else None
    if match and int(match[1]) <= 23 and int(match[2]) <= 59:
        return f"{int(match[1]):02d}:{match[2]}"
    return None


def _parse_entry(index: int, raw, problems: list[str]) -> Override | None:
    where = f"entry {index}"
    if not isinstance(raw, dict):
        problems.append(f"{where}: must be a [[train]] table")
        return None
    number = raw.get("number")
    if not (isinstance(number, str) and number.isdigit() and len(number) == 5):
        problems.append(f'{where}: number must be a 5-digit train number in quotes, e.g. "96301"')
        return None
    where = f"{where} (train {number})"
    before = len(problems)

    for key in sorted(set(raw) - _KEYS):
        problems.append(f"{where}: unknown key {key!r}")
    reason = raw.get("reason")
    if not (isinstance(reason, str) and reason.strip()):
        problems.append(f"{where}: reason is required: say what is wrong and how you know")
    action = raw.get("action", "set")
    if action not in ACTIONS:
        problems.append(f"{where}: action must be one of {', '.join(ACTIONS)}, not {action!r}")
    direction = raw.get("direction")
    if direction is not None and direction not in ("up", "down"):
        problems.append(f'{where}: direction must be "up" or "down", not {direction!r}')

    fields = {key: raw[key] for key in _TRAIN_FIELDS if key in raw}
    if "days" in fields:
        try:
            parse_days(str(fields["days"]))
        except ValueError as exc:
            problems.append(f"{where}: {exc}")
    if "service_type" in fields and fields["service_type"] not in ("fast", "slow"):
        problems.append(f'{where}: service_type must be "fast" or "slow"')
    if "service_code" in fields and not isinstance(fields["service_code"], str):
        problems.append(f'{where}: service_code must be text, e.g. "A 1"')
    if "ac" in fields and not isinstance(fields["ac"], bool):
        problems.append(f"{where}: ac must be true or false")
    if "cars" in fields and fields["cars"] not in (12, 15):
        problems.append(f"{where}: cars must be 12 or 15")
    notes = fields.get("notes", [])
    if not (isinstance(notes, list) and all(isinstance(note, str) for note in notes)):
        problems.append(f'{where}: notes must be a list of words, e.g. ["ladies_special"]')

    def station(code) -> bool:
        if isinstance(code, str) and code in _STATION_BY_CODE:
            return True
        problems.append(f"{where}: unknown station code {code!r}")
        return False

    stops = None
    if "stops" in raw:
        stops = []
        given = raw["stops"]
        if not isinstance(given, list):
            given = []
            problems.append(f'{where}: stops must be a list like [["CSMT", "08:04"], ...]')
        for item in given:
            if not (isinstance(item, list) and len(item) == 2):
                problems.append(f'{where}: each stop must be ["CODE", "HH:MM"], not {item!r}')
            elif station(item[0]):
                if (clock := _time(item[1])) is None:
                    problems.append(f"{where}: {item[1]!r} at {item[0]} is not a time like 08:04")
                else:
                    stops.append(StopTime(_STATION_BY_CODE[item[0]], clock))

    stop_times: dict[str, str] = {}
    given_times = raw.get("stop_times", {})
    if not isinstance(given_times, dict):
        given_times = {}
        problems.append(f'{where}: stop_times must be a table like {{ DIVA = "14:19" }}')
    for code, value in given_times.items():
        if station(code):
            if (clock := _time(value)) is None:
                problems.append(f"{where}: {value!r} at {code} is not a time like 08:04")
            else:
                stop_times[code] = clock
    remove_stops = raw.get("remove_stops", [])
    if not isinstance(remove_stops, list):
        remove_stops = []
        problems.append(f'{where}: remove_stops must be a list like ["VVH"]')
    remove_stops = [code for code in remove_stops if station(code)]

    if action == "add":
        for key in ("direction", "service_type", "stops"):
            if key not in raw:
                problems.append(f'{where}: action "add" needs {key}')
        if stop_times or remove_stops:
            problems.append(f'{where}: action "add" takes stops, not stop_times or remove_stops')
    elif action == "remove":
        extra = sorted(set(raw) - {"number", "reason", "action"})
        if extra:
            problems.append(f'{where}: action "remove" takes nothing else ({", ".join(extra)})')
    elif action == "set":
        if "direction" in raw:
            problems.append(f"{where}: a train's direction can't be changed; remove and add it")
        if stops is not None and (stop_times or remove_stops):
            problems.append(f"{where}: give stops, or stop_times/remove_stops, not both")
        if not (fields or stops is not None or stop_times or remove_stops):
            problems.append(f"{where}: nothing to change")

    if len(problems) > before:
        return None
    return Override(
        index, number, reason.strip(), action, direction, fields, stops, stop_times, remove_stops
    )


def parse_overrides(text: str, source: str = DEFAULT_FILE) -> list[Override]:
    """Read overrides from TOML text. Raises OverrideError listing every problem."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise OverrideError(f"{source} is not valid TOML: {exc}") from None
    problems: list[str] = []
    for key in sorted(set(data) - {"train"}):
        problems.append(f"unknown top-level key {key!r}; entries are [[train]] tables")
    entries = data.get("train", [])
    if not isinstance(entries, list):
        entries = []
        problems.append("train must be written as [[train]] tables")
    overrides = [
        override
        for index, raw in enumerate(entries, start=1)
        if (override := _parse_entry(index, raw, problems)) is not None
    ]
    seen: dict[str, int] = {}
    for override in overrides:
        if override.number in seen:
            problems.append(
                f"{override.where}: train {override.number} already has entry "
                f"{seen[override.number]}; put all its changes in one entry"
            )
        seen[override.number] = override.index
    if problems:
        raise OverrideError(f"{source} has {len(problems)} problem(s):\n  " + "\n  ".join(problems))
    return overrides


def read_overrides(path: str | Path | None = None) -> list[Override]:
    """Read the overrides file: the one shipped with the package, or `path`."""
    path = Path(path) if path is not None else default_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OverrideError(f"can't read the overrides file {path}: {exc}") from None
    return parse_overrides(text, path.name)


def _route_key(direction: str):
    """Sort key putting stations in the order a train of this direction calls at them."""
    sign = 1 if direction == "down" else -1
    return lambda stop: sign * _ORDER[stop.station.code]


def _apply_fields(train: ConvertedTrain, fields: dict) -> None:
    for key, value in fields.items():
        if key == "days":
            train.days = str(value).strip().lower() or "daily"
        elif key == "notes":
            train.notes = list(value)
        else:
            setattr(train, key, value)


def _snapshot(train: ConvertedTrain) -> tuple:
    return (
        tuple(train.stops),
        train.days,
        train.service_type,
        train.service_code,
        train.ac,
        train.cars,
        tuple(train.notes),
    )


def apply_overrides(trains: dict[str, ConvertedTrain], overrides: Sequence[Override]) -> list[str]:
    """Apply `overrides` to `trains`, in place. Returns one report line per entry.

    Nothing is changed unless every entry can be applied: a missing train, an existing one
    for "add", or stops that don't make a valid run raise OverrideError first.
    """
    problems: list[str] = []
    planned: list[tuple[Override, ConvertedTrain | None]] = []
    for override in overrides:
        current = trains.get(override.number)
        if override.action == "add":
            if current is not None:
                problems.append(
                    f'{override.where}: the train already exists; use action "set" to change it'
                )
                continue
            new = ConvertedTrain(
                number=override.number,
                direction=override.direction,
                service_type=override.fields["service_type"],
                days="daily",
                stops=list(override.stops),
                page=0,
            )
            _apply_fields(new, override.fields)
        elif current is None:
            problems.append(
                f"{override.where}: no such train in the converted timetable"
                + ('; use action "add" to supply it' if override.action == "set" else "")
            )
            continue
        elif override.action == "remove":
            planned.append((override, None))
            continue
        else:
            new = ConvertedTrain(
                number=current.number,
                direction=current.direction,
                service_type=current.service_type,
                days=current.days,
                stops=list(current.stops),
                page=current.page,
                service_code=current.service_code,
                ac=current.ac,
                cars=current.cars,
                notes=list(current.notes),
            )
            _apply_fields(new, override.fields)
            if override.stops is not None:
                new.stops = list(override.stops)
            for code in override.remove_stops:
                if not any(stop.station.code == code for stop in new.stops):
                    problems.append(f"{override.where}: the train has no stop at {code} to remove")
                new.stops = [stop for stop in new.stops if stop.station.code != code]
            for code, clock in override.stop_times.items():
                new.stops = [stop for stop in new.stops if stop.station.code != code]
                new.stops.append(StopTime(_STATION_BY_CODE[code], clock))
            if override.stop_times:
                new.stops.sort(key=_route_key(new.direction))
        codes = [stop.station.code for stop in new.stops]
        if len(set(codes)) != len(codes):
            problems.append(f"{override.where}: a station appears twice in its stops")
        elif problem := check_stops(new.stops):
            problems.append(f"{override.where}: {problem}")
        else:
            planned.append((override, new))
    if problems:
        raise OverrideError(
            f"{len(problems)} override(s) can't be applied:\n  " + "\n  ".join(problems)
        )

    lines = []
    for override, new in planned:
        number = override.number
        if new is None:
            del trains[number]
            lines.append(f"{number}: removed. Reason: {override.reason}")
        elif override.action == "add":
            trains[number] = new
            first, last = new.stops[0], new.stops[-1]
            lines.append(
                f"{number}: added, {new.service_type} {new.direction} ({first.station.name} "
                f"{first.time} - {last.station.name} {last.time}, {len(new.stops)} stops). "
                f"Reason: {override.reason}"
            )
        elif _snapshot(trains[number]) == _snapshot(new):
            lines.append(
                f"{number}: no change; the timetable already says this. "
                "The entry can probably be deleted"
            )
        else:
            trains[number] = new
            lines.append(f"{number}: changed. Reason: {override.reason}")
    return lines
