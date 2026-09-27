from pathlib import Path

import pytest

from sitt.db import init_db
from sitt.ingest.timetable import load_timetable, read_timetable

SAMPLE_CSV = Path(__file__).parent / "fixtures" / "sample_timetable.csv"


@pytest.fixture
def con():
    with init_db(":memory:") as con:
        yield con


@pytest.fixture
def loaded(con):
    """A database holding the sample timetable (invented Kalyan-CSMT trains)."""
    load_timetable(con, read_timetable(SAMPLE_CSV))
    return con
