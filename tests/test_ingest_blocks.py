"""Megablocks: Yatri's announcement cards, notice wording, storage and the CLI. No network."""

from datetime import date, time
from pathlib import Path

import httpx
import pytest

from sitt.db import init_db
from sitt.ingest import blocks
from sitt.ingest.blocks import (
    Block,
    BlockError,
    blocks_from_announcements,
    blocks_from_json,
    blocks_to_json,
    fetch_yatri,
    list_blocks,
    main,
    parse_announcements,
    parse_notice,
    save_blocks,
)
from sitt.ingest.live.common import SourceError, make_client

# Invented text in the real page's structure.
PAGE = (Path(__file__).parent / "fixtures" / "blocks" / "yatri_announcements.html").read_text(
    encoding="utf-8"
)
SUNDAY = date(2026, 10, 11)


def test_cards_are_read_from_the_page():
    cards = parse_announcements(PAGE)
    assert [card.category for card in cards] == ["2", "2", "1", "2", "2"]
    assert cards[0].title.startswith("Megablock on Central and Harbour line on Sunday, 11th")
    assert cards[0].posted == "Sat, 10 Oct 26 10:40AM"
    assert parse_announcements("<html><body>nothing here</body></html>") == []


def test_blocks_from_cards_have_a_date_and_line_only():
    found, warnings = blocks_from_announcements(parse_announcements(PAGE))
    assert [(b.block_date, b.line) for b in found] == [
        (SUNDAY, "central"),
        (SUNDAY, "harbour"),
        (date(2026, 10, 10), "western"),  # "10th/11th October": the night it starts
        (date(2027, 1, 3), "transharbour"),  # posted 31 December, so next year
    ]
    for block in found:
        assert block.source == "yatri" and not block.detailed
        assert (block.from_station, block.start_time, block.tracks) == (None, None, None)
        assert "Invented" not in block.summary  # the card's text is not kept
    # The cancellation card is ignored; the block with no date or line is reported.
    assert warnings == ["a block announcement posted 2026-10-16 has no readable date"]


def test_fetch_makes_one_polite_request():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=PAGE)

    with make_client(httpx.MockTransport(handler)) as client:
        found, warnings, log = fetch_yatri(client)
    assert len(seen) == 1 and str(seen[0].url) == blocks.YATRI_URL
    assert "should-i-take-this-train" in seen[0].headers["user-agent"]
    assert len(found) == 4 and len(warnings) == 1 and len(log) == 1

    with make_client(httpx.MockTransport(lambda r: httpx.Response(200, text="<p>x</p>"))) as c:
        assert fetch_yatri(c)[1] == [
            "no announcement cards found; the page layout may have changed"
        ]
    with (
        make_client(httpx.MockTransport(lambda r: httpx.Response(503))) as c,
        pytest.raises(SourceError),
    ):
        fetch_yatri(c)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Matunga-Mulund Up and Down fast lines from 11.05 am to 3.55 pm",
            [("central", "MTN", "MLND", time(11, 5), time(15, 55), "fast", "both")],
        ),
        (
            "Thane - Kalyan Up & Dn slow lines from 10.40 am to 3.40 pm and "
            "Panvel-Vashi Up and Down harbour lines from 11.05 am to 4.05 pm",
            [
                ("central", "TNA", "KYN", time(10, 40), time(15, 40), "slow", "both"),
                ("harbour", "Panvel", "Vashi", time(11, 5), time(16, 5), None, "both"),
            ],
        ),
        (
            "Byculla to Vidyavihar Dn slow line from 23.30 hrs to 04.30 hrs",
            [("central", "BY", "VVH", time(23, 30), time(4, 30), "slow", "down")],
        ),
        (
            "on the CSMT - Kurla Up fast line from 00:15 to 03:45",
            [("central", "CSMT", "CLA", time(0, 15), time(3, 45), "fast", "up")],
        ),
        ("Trains will be diverted on Sunday.", []),
    ],
)
def test_parse_notice(text, expected):
    found = parse_notice(text, SUNDAY)
    assert [
        (b.line, b.from_station, b.to_station, b.start_time, b.end_time, b.tracks, b.direction)
        for b in found
    ] == expected
    assert all(b.block_date == SUNDAY and b.source == "manual" for b in found)
    assert all(text not in (b.summary or "") for b in found)


def test_parse_notice_rejects_impossible_times():
    with pytest.raises(BlockError, match="can't read the time"):
        parse_notice("Thane-Kalyan Up fast line from 13.30 pm to 3.40 pm", SUNDAY)


def test_block_ids_are_stable_and_distinguish_blocks():
    fast = Block(SUNDAY, "central", "MTN", "MLND", time(11, 5), time(15, 55), "fast", "both")
    again = Block(SUNDAY, "central", "MTN", "MLND", time(11, 5), time(15, 55), "fast", "both")
    slow = Block(SUNDAY, "central", "MTN", "MLND", time(11, 5), time(15, 55), "slow", "both")
    assert fast.block_id == again.block_id != slow.block_id
    assert fast.block_id.startswith("20261011-central-")
    assert fast.describe() == "Sun 11 Oct 2026, central, MTN-MLND, fast lines, both, 11:05-15:55"


def test_saving_is_idempotent_and_details_replace_date_only_blocks(con):
    vague = Block(SUNDAY, "central", source="yatri")
    harbour = Block(SUNDAY, "harbour", source="yatri")
    assert save_blocks(con, [vague, harbour]) == 2
    assert save_blocks(con, [vague, harbour]) == 0

    detailed = Block(SUNDAY, "central", "MTN", "MLND", time(11, 5), time(15, 55), "fast", "both")
    assert save_blocks(con, [detailed]) == 1
    stored = list_blocks(con)
    assert [(b.line, b.detailed) for b in stored] == [("central", True), ("harbour", False)]
    # Fetching the announcement again doesn't bring the date-only block back.
    assert save_blocks(con, [vague]) == 0
    assert save_blocks(con, [detailed]) == 0
    assert len(list_blocks(con)) == 2
    assert list_blocks(con, from_date=date(2026, 10, 12)) == []


def test_json_round_trip():
    found = parse_notice("Byculla to Vidyavihar Dn slow line from 23.30 hrs to 04.30 hrs", SUNDAY)
    found.append(Block(SUNDAY, "western", source="yatri"))
    assert blocks_from_json(blocks_to_json(found)) == found


def test_cli(tmp_path, capsys, monkeypatch):
    db = tmp_path / "test.duckdb"
    run = lambda *args: main(["--db", str(db), *args])  # noqa: E731

    assert (
        run(
            "add",
            "--date",
            "2026-10-11",
            "--from",
            "Matunga",
            "--to",
            "MLND",
            "--start",
            "11:05",
            "--end",
            "15:55",
            "--tracks",
            "fast",
            "--direction",
            "both",
        )
        == 0
    )
    out = capsys.readouterr().out
    assert out.startswith(
        "Added: Sun 11 Oct 2026, central, MTN-MLND, fast lines, both, 11:05-15:55"
    )
    block_id = out.strip().rsplit("[", 1)[1].rstrip("]")
    assert (
        run(
            "add",
            "--date",
            "2026-10-11",
            "--from",
            "MTN",
            "--to",
            "MLND",
            "--start",
            "11:05",
            "--end",
            "15:55",
            "--tracks",
            "fast",
            "--direction",
            "both",
        )
        == 0
    )
    assert capsys.readouterr().out.startswith("Already stored")

    text = "Thane-Kalyan Up and Down slow lines from 10.40 to 15.40"
    assert run("parse", "--date", "2026-10-18", "--dry-run", text) == 0
    assert "database not changed" in capsys.readouterr().out
    assert run("parse", "--date", "2026-10-18", text) == 0
    assert "1 new" in capsys.readouterr().out
    assert run("parse", "--date", "2026-10-18", "no block in here") == 1
    assert "couldn't find a block" in capsys.readouterr().err

    assert run("list") == 0
    listing = capsys.readouterr().out
    assert "2 block(s)." in listing and "TNA-KYN" in listing and "(manual)" in listing
    assert run("list", "--from-date", "2026-10-15") == 0
    assert "1 block(s)." in capsys.readouterr().out

    assert run("remove", block_id) == 0
    assert run("remove", block_id) == 1

    # fetch: one request, served here by a fake transport.
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=PAGE))
    monkeypatch.setattr(blocks, "make_client", lambda: make_client(transport))
    out_file = tmp_path / "data" / "blocks" / "2026-10-10.json"
    assert run("fetch", "--no-db", "--json-out", str(out_file)) == 0
    assert "Found 4 block(s); database not changed." in capsys.readouterr().out
    assert len(blocks_from_json(out_file.read_text(encoding="utf-8"))) == 4
    assert run("fetch") == 0
    assert "Found 4 block(s); 4 new." in capsys.readouterr().out
    assert run("fetch", "--dry-run") == 0
    assert run("load", str(out_file.parent)) == 0
    assert "Read 4 block(s) from 1 file(s); 0 new." in capsys.readouterr().out.splitlines()[-1]
    assert run("load", str(tmp_path / "nowhere")) == 1

    with init_db(db) as con:
        assert len(list_blocks(con)) == 5
