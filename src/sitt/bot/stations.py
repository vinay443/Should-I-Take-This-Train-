"""Hardcoded Central line stations between Kalyan and CSMT.

Stopgap until timetable ingestion populates the `stations` table; swap
`lookup_station` and `STATIONS` for queries against that table then.
"""

# TODO: Replace this map with the `stations` table (src/sitt/db/schema.sql), which
# sitt.ingest.timetable now fills; sitt.timetable.resolve_station already looks up
# codes and names there. The aliases below ("cst", "vt", "dombivali", ...) have no
# home in that table yet, so decide where they live before switching.

from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class Station:
    code: str
    name: str
    aliases: tuple[str, ...] = ()


# Ordered Kalyan -> CSMT, the usual morning commute direction.
STATIONS: tuple[Station, ...] = (
    Station("KYN", "Kalyan"),
    Station("THK", "Thakurli"),
    Station("DI", "Dombivli", ("dombivali",)),
    Station("KOPR", "Kopar"),
    Station("DIVA", "Diva"),
    Station("MBQ", "Mumbra"),
    Station("KLVA", "Kalwa"),
    Station("TNA", "Thane"),
    Station("MLND", "Mulund"),
    Station("NHU", "Nahur"),
    Station("BND", "Bhandup"),
    Station("KJRD", "Kanjur Marg", ("kanjur",)),
    Station("VK", "Vikhroli"),
    Station("GC", "Ghatkopar"),
    Station("VVH", "Vidyavihar"),
    Station("CLA", "Kurla"),
    Station("SION", "Sion", ("sin",)),  # SIN was this bot's code before the PDF timetable
    Station("MTN", "Matunga"),
    Station("DR", "Dadar"),
    Station("PR", "Parel"),
    Station("CRD", "Currey Road", ("currey",)),
    Station("CHG", "Chinchpokli"),
    Station("BY", "Byculla"),
    Station("SNRD", "Sandhurst Road", ("sandhurst",)),
    Station("MSD", "Masjid", ("masjidbunder",)),
    Station("CSMT", "CSMT", ("cst", "vt")),
)

STATIONS_BY_CODE: dict[str, Station] = {s.code: s for s in STATIONS}


def lookup_keys(station: Station) -> Iterator[str]:
    """Lowercase tokens that identify `station`: code, name without spaces, aliases."""
    yield station.code.lower()
    yield station.name.lower().replace(" ", "")
    yield from (alias.lower() for alias in station.aliases)


_LOOKUP: dict[str, Station] = {key: s for s in STATIONS for key in lookup_keys(s)}


def lookup_station(token: str) -> Station | None:
    """Find a station by code, name or alias, case-insensitively."""
    return _LOOKUP.get(token.strip().lower())
