"""The collector's run log: one `collector_runs` row per run per source.

`sitt-collect` writes these rows after every run, including runs that fetched nothing.
`sitt.health` reads them to tell a missed run (no rows: the machine was off or asleep)
from a failed source (a 'failed' row).

Writing the log must never get in the way of collecting. If the database can't be
written, the rows are appended to a small JSON-lines file beside the collector's log and
moved into the table by the next run that can open the database.
"""

import json
import logging
import re
import socket
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from sitt.ingest.live.collect import SOURCES, CollectResult

logger = logging.getLogger(__name__)

OK, PARTIAL, FAILED, SKIPPED_DISABLED = "ok", "partial", "failed", "skipped_disabled"
STATUSES = (OK, PARTIAL, FAILED, SKIPPED_DISABLED)

PENDING_FILE = "collector_runs.pending.jsonl"
NOT_LOADED = "not loaded into the database"
ERROR_LENGTH = 200

_QUERY_RE = re.compile(r"\?[^\s'\"]*")
_BATCH_RE = re.compile(r"^(\d{8}T\d{6})Z-")


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    source: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    readings: int = 0
    trains_matched: int | None = None
    trains_unmatched: int | None = None
    error: str | None = None
    host: str | None = None
    backfilled: bool = False


COLUMNS = tuple(RunRecord.__dataclass_fields__)


def host_name() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


def short_error(message: str | None) -> str | None:
    """A one-line error fit to store: no query strings (tokens live there), and short."""
    if not message:
        return None
    text = " ".join(_QUERY_RE.sub("", str(message)).split())
    return text if len(text) <= ERROR_LENGTH else text[: ERROR_LENGTH - 1] + "…"


def build_records(
    collected: CollectResult,
    *,
    started_at: datetime,
    finished_at: datetime,
    load_error: str | None = None,
    host: str | None = None,
    all_sources: Iterable[str] = tuple(SOURCES),
) -> list[RunRecord]:
    """One record per known source: what ran, what failed, and what is switched off."""
    host = host or host_name()
    records = []
    ran = {result.source: result for result in collected.sources}
    for source in all_sources:
        common = dict(
            run_id=collected.batch_id,
            source=source,
            started_at=started_at,
            finished_at=finished_at,
            host=host,
        )
        result = ran.get(source)
        if result is None:
            records.append(RunRecord(status=SKIPPED_DISABLED, **common))
        elif not result.ok:
            records.append(RunRecord(status=FAILED, error=short_error(result.error), **common))
        elif not result.observations:
            records.append(RunRecord(status=PARTIAL, error="no readings parsed", **common))
        elif load_error is not None:
            records.append(
                RunRecord(
                    status=PARTIAL,
                    readings=len(result.observations),
                    error=short_error(f"{NOT_LOADED}: {load_error}"),
                    **common,
                )
            )
        else:
            records.append(RunRecord(status=OK, readings=len(result.observations), **common))
    return records


def match_counts(con: duckdb.DuckDBPyConnection, run_id: str) -> dict[str, tuple[int, int]]:
    """Per source: distinct trains of this run that are (matched, unmatched) to the timetable.

    A reading is unmatched while its `train_id` is still the raw number (see load.rematch).
    """
    rows = con.execute(
        """
        SELECT source,
               count(DISTINCT train_number) FILTER (WHERE train_id <> train_number),
               count(DISTINCT train_number) FILTER (WHERE train_id = train_number)
        FROM observations WHERE batch_id = ? GROUP BY source
        """,
        [run_id],
    ).fetchall()
    return {source: (matched, unmatched) for source, matched, unmatched in rows}


def with_match_counts(
    con: duckdb.DuckDBPyConnection, records: Sequence[RunRecord]
) -> list[RunRecord]:
    """Fill in the matched/unmatched counts, and promote records whose batch has since loaded."""
    out = []
    counts: dict[str, dict[str, tuple[int, int]]] = {}
    for record in records:
        if record.status not in (OK, PARTIAL) or record.readings == 0:
            out.append(record)
            continue
        if record.run_id not in counts:
            counts[record.run_id] = match_counts(con, record.run_id)
        found = counts[record.run_id].get(record.source)
        if found is None:
            out.append(record)
            continue
        loaded_late = record.status == PARTIAL and (record.error or "").startswith(NOT_LOADED)
        out.append(
            replace(
                record,
                trains_matched=found[0],
                trains_unmatched=found[1],
                status=OK if loaded_late else record.status,
                error=None if loaded_late else record.error,
            )
        )
    return out


def write_records(con: duckdb.DuckDBPyConnection, records: Sequence[RunRecord]) -> int:
    if not records:
        return 0
    con.executemany(
        f"INSERT OR REPLACE INTO collector_runs ({', '.join(COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in COLUMNS)})",
        [[getattr(record, name) for name in COLUMNS] for record in records],
    )
    return len(records)


def backfill(con: duckdb.DuckDBPyConnection) -> int:
    """Reconstruct run rows for batches collected before the run log existed.

    Only what the observations prove is recorded: that the source answered and how many
    readings it gave. The start time is the one in the batch ID. Rows are marked
    `backfilled`. Synthetic batches are never backfilled.
    """
    rows = con.execute(
        """
        SELECT batch_id, source, count(*),
               count(DISTINCT train_number) FILTER (WHERE train_id <> train_number),
               count(DISTINCT train_number) FILTER (WHERE train_id = train_number)
        FROM observations
        WHERE batch_id IS NOT NULL AND source <> 'synthetic'
          AND batch_id NOT IN (SELECT run_id FROM collector_runs)
        GROUP BY batch_id, source
        """
    ).fetchall()
    records = []
    for batch_id, source, readings, matched, unmatched in rows:
        stamp = _BATCH_RE.match(batch_id)
        if stamp is None:
            continue
        started = datetime.strptime(stamp[1], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
        records.append(
            RunRecord(batch_id, source, started, None, OK, readings, matched, unmatched, None, None)
        )
    return write_records(con, [replace(record, backfilled=True) for record in records])


# --- the fallback file, for when the database can't be written ---


def _to_json(record: RunRecord) -> str:
    row = asdict(record)
    for key in ("started_at", "finished_at"):
        row[key] = row[key].isoformat() if row[key] else None
    return json.dumps(row, ensure_ascii=False)


def _from_json(line: str) -> RunRecord:
    row = json.loads(line)
    for key in ("started_at", "finished_at"):
        row[key] = datetime.fromisoformat(row[key]) if row[key] else None
    return RunRecord(**{name: row.get(name) for name in COLUMNS if name in row})


def append_pending(log_dir: Path, records: Sequence[RunRecord]) -> Path:
    path = Path(log_dir) / PENDING_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for record in records:
            f.write(_to_json(record) + "\n")
    return path


def read_pending(log_dir: Path) -> list[RunRecord]:
    path = Path(log_dir) / PENDING_FILE
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(_from_json(line))
        except (ValueError, TypeError):
            logger.warning("skipping an unreadable line in %s", path)
    return records


def clear_pending(log_dir: Path) -> None:
    (Path(log_dir) / PENDING_FILE).unlink(missing_ok=True)


def record_run(
    con: duckdb.DuckDBPyConnection, records: Sequence[RunRecord], log_dir: Path | None = None
) -> int:
    """Write this run's rows, any rows left in the fallback file, and any backfill.

    Never raises: a problem here is logged and the rows go to the fallback file instead.
    """
    try:
        pending = read_pending(log_dir) if log_dir is not None else []
        written = write_records(con, with_match_counts(con, [*pending, *records]))
        if pending:
            clear_pending(log_dir)
        backfill(con)
        return written
    except Exception:  # the run log must never break collection
        logger.exception("could not write the collector run log")
        save_for_later(records, log_dir)
        return 0


def save_for_later(records: Sequence[RunRecord], log_dir: Path | None) -> None:
    """Append rows to the fallback file. Never raises."""
    if log_dir is None:
        return
    try:
        append_pending(log_dir, records)
    except Exception:
        logger.exception("could not save the collector run log to %s", log_dir)
