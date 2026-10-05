"""Run the collector once on this machine and append to the local database.

    uv run sitt-collect

For when GitHub's runners can't reach the sources (see docs/collector.md). One run:

1. fetches each enabled source once (the same code and manners as the GitHub workflow),
2. writes the raw responses and one Parquet batch under `data/`,
3. loads every batch not yet in `data/sitt.duckdb` into its `observations` table.

Sources: NTES always. Mobond only when `SITT_MOBOND_ENABLED=true` is set in the
environment or `.env`; it is **off by default**, exactly as in the workflow, because
Mobond hasn't agreed to being polled.

If the database is busy (the bot or the dashboard has it open for writing), the load is
retried a few times and then skipped. Nothing is lost: the Parquet batch stays on disk and
the next run loads it.

Every run, including one where every source failed, is recorded in `collector_runs`
(see sitt.ingest.live.runlog), which is what `sitt-health` reads.

Meant to be run every 15 minutes by Windows Task Scheduler; `scripts/collect-once.ps1`
wraps it, and `scripts/register-collector-task.ps1` creates the task.
"""

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import duckdb
import httpx
from dotenv import find_dotenv, load_dotenv

from sitt.config import load_settings
from sitt.db import init_db
from sitt.ingest.live import mobond, ntes, runlog
from sitt.ingest.live.__main__ import summarise
from sitt.ingest.live.collect import CollectResult, collect
from sitt.ingest.live.common import make_client
from sitt.ingest.live.load import LoadResult, load

logger = logging.getLogger("sitt.collect")

MOBOND_SWITCH = "SITT_MOBOND_ENABLED"
LOAD_ATTEMPTS = 4
LOAD_RETRY_SECONDS = 5.0


def enabled_sources(environ: dict[str, str] | None = None) -> list[str]:
    """NTES, plus Mobond only if the switch is exactly 'true' (as in collect.yml)."""
    environ = os.environ if environ is None else environ
    sources = [ntes.SOURCE]
    if environ.get(MOBOND_SWITCH, "").strip().lower() == "true":
        sources.insert(0, mobond.SOURCE)
    return sources


@dataclass
class LocalRun:
    collected: CollectResult
    loaded: LoadResult | None
    load_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.collected.ok and self.load_error is None


def with_database[T](
    db_path: Path,
    action: Callable[[duckdb.DuckDBPyConnection], T],
    attempts: int = LOAD_ATTEMPTS,
    wait_seconds: float = LOAD_RETRY_SECONDS,
) -> T:
    """Run `action` on the database, waiting out another process that has it open."""
    for attempt in range(1, attempts + 1):
        try:
            with init_db(db_path) as con:
                return action(con)
        except duckdb.IOException as exc:
            if attempt == attempts:
                raise
            logger.warning("database busy (%s); retrying in %ss", exc, wait_seconds)
            time.sleep(wait_seconds)
    raise AssertionError("unreachable")


def load_with_retry(
    db_path: Path,
    observations_dir: Path,
    attempts: int = LOAD_ATTEMPTS,
    wait_seconds: float = LOAD_RETRY_SECONDS,
) -> LoadResult:
    """Load new batches, waiting out another process that has the database open."""
    return with_database(db_path, lambda con: load(con, observations_dir), attempts, wait_seconds)


def run_once(
    data_dir: Path,
    db_path: Path,
    sources: Sequence[str],
    client: httpx.Client | None = None,
    wait_seconds: float = LOAD_RETRY_SECONDS,
) -> LocalRun:
    """One collection, one load, and one entry per source in the run log."""
    observations_dir = data_dir / "observations"
    log_dir = data_dir / "logs"
    started_at = datetime.now(UTC)
    own = client is None
    client = client or make_client()
    try:
        collected = collect(
            sources, client, observations_dir=observations_dir, raw_dir=data_dir / "raw"
        )
    finally:
        if own:
            client.close()
    finished_at = datetime.now(UTC)

    def records(load_error: str | None = None) -> list[runlog.RunRecord]:
        return runlog.build_records(
            collected, started_at=started_at, finished_at=finished_at, load_error=load_error
        )

    def load_and_record(con: duckdb.DuckDBPyConnection) -> LoadResult | None:
        loaded = load(con, observations_dir) if observations_dir.is_dir() else None
        runlog.record_run(con, records(), log_dir)
        return loaded

    try:
        loaded = with_database(db_path, load_and_record, wait_seconds=wait_seconds)
    except duckdb.Error as exc:
        error = f"{type(exc).__name__}: {exc}"
        runlog.save_for_later(records(error), log_dir)
        return LocalRun(collected, None, error)
    return LocalRun(collected, loaded)


def _configure_logging(log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        )
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=os.environ.get("SITT_LOG_LEVEL") or "INFO",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sitt-collect",
        description="Collect live running status once and append it to the local database.",
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--db", type=Path, help="default: SITT_DB_PATH or data/sitt.duckdb")
    parser.add_argument(
        "--log-file",
        type=Path,
        help="append a log here as well as printing it (default: DATA_DIR/logs/collector.log)",
    )
    parser.add_argument("--no-log-file", action="store_true", help="only print the log")
    args = parser.parse_args(argv)

    load_dotenv(find_dotenv(usecwd=True))
    log_file = (
        None if args.no_log_file else args.log_file or args.data_dir / "logs" / "collector.log"
    )
    _configure_logging(log_file)

    sources = enabled_sources()
    if mobond.SOURCE not in sources:
        logger.info("Mobond is off (set %s=true to enable it)", MOBOND_SWITCH)
    db_path = args.db or load_settings().db_path
    run = run_once(args.data_dir, db_path, sources)
    for line in summarise(run.collected, dry_run=False).splitlines():
        logger.info(line)
    if run.loaded is not None:
        logger.info(
            "loaded %d rows from %d new batch(es) into %s; matched %d train and %d station "
            "reference(s)",
            run.loaded.rows,
            run.loaded.files,
            db_path,
            run.loaded.trains_matched,
            run.loaded.stations_matched,
        )
    if run.load_error:
        logger.error(
            "could not load into %s (%s). The batch is on disk and will load next run.",
            db_path,
            run.load_error,
        )
    return 0 if run.ok else 1


if __name__ == "__main__":
    sys.exit(main())
