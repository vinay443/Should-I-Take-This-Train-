"""sitt-timetable-reload: find the PDFs by hash, back up, convert and load."""

import hashlib
import multiprocessing
from pathlib import Path

import duckdb
import pytest
from conftest import SAMPLE_CSV

from sitt.db import init_db
from sitt.ingest import cr_pdf, reload


def fake_pdf(folder: Path, name: str, content: bytes) -> tuple[Path, str]:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


@pytest.fixture
def pdfs(tmp_path, monkeypatch):
    """Five stand-in PDFs whose hashes are registered as the known editions."""
    folder = tmp_path / "scratch"
    dn, dn_hash = fake_pdf(folder, "ptt/dn.pdf", b"down")
    up, up_hash = fake_pdf(folder, "cr-main-up.pdf", b"up")
    ac, ac_hash = fake_pdf(folder, "cr-ac.pdf", b"ac")
    cars, cars_hash = fake_pdf(folder, "cr-15car.pdf", b"cars")
    hb, hb_hash = fake_pdf(folder, "part10/cr-harbour-dn.pdf", b"harbour")
    fake_pdf(folder, "notes.pdf", b"something else")
    monkeypatch.setattr(
        cr_pdf,
        "KNOWN_SOURCES",
        {
            dn_hash: ("2024-10-05", "u", "main"),
            up_hash: ("2024-10-05", "u", "main"),
            ac_hash: ("2025-04-16", "u", "ac"),
            cars_hash: ("2026-08-15", "u", "15car"),
        },
    )
    monkeypatch.setattr(reload, "MAIN_ORDER", (dn_hash, up_hash))
    monkeypatch.setattr(reload, "HARBOUR_SOURCES", {hb_hash: ("2026-05-01", "harbour")})
    return folder


def test_pdfs_are_found_by_their_hash_wherever_they_are(pdfs):
    found = reload.find_pdfs(pdfs)
    assert [p.name for p in found.main] == ["dn.pdf", "cr-main-up.pdf"]  # DOWN, then UP
    assert (found.ac.name, found.cars.name) == ("cr-ac.pdf", "cr-15car.pdf")
    assert found.harbour.name == "cr-harbour-dn.pdf" and found.harbour_edition == "2026-05-01"
    assert [p.name for p in found.unknown] == ["notes.pdf"]


def test_without_both_main_line_pdfs_nothing_changes(pdfs, tmp_path):
    (pdfs / "ptt" / "dn.pdf").unlink()
    db = tmp_path / "sitt.duckdb"
    init_db(db).close()
    said = []
    assert reload.reload(pdfs, db, out=said.append) == 1
    assert "Both main line PDFs are needed" in said[-1]
    assert list(tmp_path.glob("sitt-before-reload-*")) == []


def _hold(db: str, ready, done) -> None:
    con = duckdb.connect(db)
    ready.set()
    done.wait(30)
    con.close()


def test_a_busy_database_is_left_alone(pdfs, tmp_path):
    db = tmp_path / "sitt.duckdb"
    init_db(db).close()
    ctx = multiprocessing.get_context("spawn")
    ready, done = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold, args=(str(db), ready, done))
    holder.start()
    try:
        assert ready.wait(30)
        said = []
        assert reload.reload(pdfs, db, out=said.append) == 2
        assert "is in use. Stop sitt-web" in said[-1]
        assert list(tmp_path.glob("sitt-before-reload-*")) == []
    finally:
        done.set()
        holder.join(30)


def test_a_reload_backs_up_then_loads_each_line_and_keeps_readings(pdfs, tmp_path, monkeypatch):
    db = tmp_path / "sitt.duckdb"
    with init_db(db) as con:
        con.execute(
            "INSERT INTO observations (observed_at, train_id, station_code, "
            "actual_or_expected_time, time_kind, delay_minutes, source, event) "
            "VALUES (now(), 'central-90106', 'KYN', now(), 'expected', 3, 'ntes', 'departure')"
        )
    converted = []

    def fake_convert(paths, line, out, edition=None, ac=None, cars=None):
        converted.append((line, [p.name for p in paths], edition, ac and ac.name))
        if line == "main":
            out.write_text("# line: central\n" + SAMPLE_CSV.read_text(encoding="utf-8"))
            return {}
        from conftest import harbour_csv

        out.write_text(harbour_csv(tmp_path).read_text(encoding="utf-8"), encoding="utf-8")
        return {"99003": "page 1: 'TNA' (Thane) in the CSMT row: runs beyond the table"}

    monkeypatch.setattr(reload, "_convert", fake_convert)
    said = []
    assert reload.reload(pdfs, db, out=said.append) == 0
    assert converted == [
        ("main", ["dn.pdf", "cr-main-up.pdf"], None, "cr-ac.pdf"),
        ("harbour", ["cr-harbour-dn.pdf"], "2026-05-01", None),
    ]
    (copy,) = tmp_path.glob("sitt-before-reload-*.duckdb")
    with duckdb.connect(str(copy), read_only=True) as before:
        assert before.execute("SELECT count(*) FROM trains").fetchone()[0] == 0
    text = "\n".join(said)
    assert "1 train (99003): 'TNA' (Thane) in the CSMT row" in text
    assert "readings 1 → 1" in text and "harbour 0 → " in text
    with duckdb.connect(str(db), read_only=True) as con:
        lines = dict(con.execute("SELECT line, count(*) FROM trains GROUP BY line").fetchall())
        assert set(lines) == {"central", "harbour"}
        assert con.execute("SELECT count(*) FROM observations").fetchone()[0] == 1


def test_cli_dry_run_only_lists(pdfs, tmp_path, capsys):
    db = tmp_path / "sitt.duckdb"
    init_db(db).close()
    assert reload.main([str(pdfs), "--db", str(db), "--dry-run"]) == 0
    assert "Harbour line DOWN: cr-harbour-dn.pdf" in capsys.readouterr().out
    assert list(tmp_path.glob("sitt-before-reload-*")) == []
    assert reload.main([str(tmp_path / "nowhere")]) == 1


def test_backups_never_overwrite(tmp_path):
    from datetime import datetime

    db = tmp_path / "sitt.duckdb"
    init_db(db).close()
    when = datetime(2026, 10, 17, 19, 30)
    first, second = reload.backup(db, when), reload.backup(db, when)
    assert first != second and first.exists() and second.exists()
