"""Station lookup for the bot: the timetable's `stations` table plus a few aliases.

The stations come from the database, which sitt.ingest.timetable fills, so every
station in the loaded timetable works. Before a timetable is loaded the table is empty,
and the bot falls back to `FALLBACK_STATIONS`, the Kalyan-CSMT stretch.

What a rider types is matched in this order, ignoring case, spaces and punctuation:
an alias in `ALIASES`, then a station code, then a station name.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb

from sitt.db import connect


@dataclass(frozen=True)
class Station:
    code: str
    name: str


# Used only while the `stations` table is empty. Ordered from the CSMT end, like
# `stations.seq`, with the timetable's codes and spellings.
FALLBACK_STATIONS: tuple[Station, ...] = (
    Station("CSMT", "CSMT"),
    Station("MSD", "Masjid"),
    Station("SNRD", "Sandhurst Road"),
    Station("BY", "Byculla"),
    Station("CHG", "Chinchpokli"),
    Station("CRD", "Currey Road"),
    Station("PR", "Parel"),
    Station("DR", "Dadar"),
    Station("MTN", "Matunga"),
    Station("SION", "Sion"),
    Station("CLA", "Kurla"),
    Station("VVH", "Vidyavihar"),
    Station("GC", "Ghatkopar"),
    Station("VK", "Vikhroli"),
    Station("KJRD", "Kanjur Marg"),
    Station("BND", "Bhandup"),
    Station("NHU", "Nahur"),
    Station("MLND", "Mulund"),
    Station("TNA", "Thane"),
    Station("KLVA", "Kalva"),
    Station("MBQ", "Mumbra"),
    Station("DIVA", "Diva"),
    Station("KOPR", "Kopar"),
    Station("DI", "Dombivli"),
    Station("THK", "Thakurli"),
    Station("KYN", "Kalyan"),
)

# Other things riders type, mapped to station codes. Keys are in `_key` form (lower
# case, letters and digits only). An alias whose station isn't in the directory is
# ignored, so this can list stations beyond the fallback stretch.
ALIASES: dict[str, str] = {
    "cst": "CSMT",
    "vt": "CSMT",
    "cstm": "CSMT",
    "chhatrapatishivajimaharajterminus": "CSMT",
    "masjidbunder": "MSD",
    "sandhurst": "SNRD",
    "currey": "CRD",
    "sin": "SION",  # the older code, still on Wikipedia; NTES uses SION
    "kanjur": "KJRD",
    "kalwa": "KLVA",
    "diwa": "DIVA",
    "dombivali": "DI",
    "dombivili": "DI",
    "kalyanjn": "KYN",
    "ulhas": "ULNR",
    "ambarnath": "ABH",
    "bhivpuri": "BVS",
    "titvala": "TLA",
    "umbermalli": "OMB",
    "oombermali": "OMB",
}


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


class StationDirectory:
    """The stations the bot knows about, in line order from the CSMT end."""

    def __init__(self, stations: Sequence[Station]):
        self.stations: tuple[Station, ...] = tuple(stations)
        self._by_code = {s.code: s for s in self.stations}
        by_name = {_key(s.name): s for s in self.stations}
        by_code_key = {_key(s.code): s for s in self.stations}
        aliases = {
            alias: self._by_code[code] for alias, code in ALIASES.items() if code in self._by_code
        }
        # Later dicts win, so the order here is the reverse of the matching order.
        self._lookup = by_name | by_code_key | aliases

    def lookup(self, text: str) -> Station | None:
        """Find a station by alias, code or name. Case, spaces and punctuation are ignored."""
        return self._lookup.get(_key(text))

    def get(self, code: str) -> Station | None:
        return self._by_code.get(code)

    def label(self, code: str) -> str:
        """`Kalyan (KYN)` for a known code, else the code itself."""
        station = self._by_code.get(code)
        if station is None or station.name == code:
            return code
        return f"{station.name} ({code})"


FALLBACK_DIRECTORY = StationDirectory(FALLBACK_STATIONS)


def directory_from(con: duckdb.DuckDBPyConnection) -> StationDirectory:
    """The directory for the `stations` table, or the fallback list if the table is empty."""
    rows = con.execute("SELECT code, name FROM stations ORDER BY seq, code").fetchall()
    if not rows:
        return FALLBACK_DIRECTORY
    return StationDirectory([Station(code, name) for code, name in rows])


def load_directory(db_path: Path | str) -> StationDirectory:
    """Read the station directory from the database. The schema must already exist."""
    with connect(db_path) as con:
        return directory_from(con)
