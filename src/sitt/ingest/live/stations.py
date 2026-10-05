"""Map Mobond's station names to Indian Railways station codes.

Mobond reports positions by name ("At ULHAS NAGAR"). This covers the busiest Central
line names, using the same codes as the timetable (`sitt.ingest.cr_pdf`). Anything else
is stored as the raw name, and `sitt.ingest.live.load.rematch` retries it against the
`stations` table once the timetable is loaded.
"""

import re

# Keys are names with everything but letters removed, so "ULHAS NAGAR" == "ULHASNAGAR".
_CENTRAL_CODES = {
    "CSMT": "CSMT",
    "MASJID": "MSD",
    "MASJIDBUNDER": "MSD",
    "SANDHURST": "SNRD",
    "SANDHURSTROAD": "SNRD",
    "BYCULLA": "BY",
    "CHINCHPOKLI": "CHG",
    "CURREY": "CRD",
    "CURREYROAD": "CRD",
    "PAREL": "PR",
    "DADAR": "DR",
    "MATUNGA": "MTN",
    "SION": "SION",
    "KURLA": "CLA",
    "VIDYAVIHAR": "VVH",
    "GHATKOPAR": "GC",
    "VIKHROLI": "VK",
    "KANJUR": "KJRD",
    "KANJURMARG": "KJRD",
    "BHANDUP": "BND",
    "NAHUR": "NHU",
    "MULUND": "MLND",
    "THANE": "TNA",
    "KALVA": "KLVA",
    "KALWA": "KLVA",
    "MUMBRA": "MBQ",
    "DIVA": "DIVA",
    "KOPAR": "KOPR",
    "DOMBIVLI": "DI",
    "THAKURLI": "THK",
    "KALYAN": "KYN",
    # Kasara branch
    "SHAHAD": "SHAD",
    "AMBIVLI": "ABY",
    "TITWALA": "TLA",
    "VASIND": "VSD",
    "ASANGAON": "ASO",
    "KASARA": "KSRA",
    # Karjat / Khopoli branch
    "VITHALWADI": "VLDI",
    "ULHASNAGAR": "ULNR",
    "AMBARNATH": "ABH",
    "BADLAPUR": "BUD",
    "NERAL": "NRL",
    "BHIVPURI": "BVS",
    "BHIVPURIROAD": "BVS",
    "KARJAT": "KJT",
    "KHOPOLI": "KHPI",
}

# Western Railway suburban numbers. WR has its own "DADAR" (code DDR), so these trains
# are never mapped through the Central line table above.
_WESTERN_PREFIXES = ("90", "91", "92", "93", "94")


def normalise_name(name: str) -> str:
    return re.sub(r"[^A-Z]", "", name.upper())


def resolve_station(name: str, train_number: str) -> str:
    """The station code for a Mobond name, or the cleaned-up name if it isn't known."""
    if not train_number.startswith(_WESTERN_PREFIXES):
        code = _CENTRAL_CODES.get(normalise_name(name))
        if code is not None:
            return code
    return " ".join(name.split()).upper()
