"""CLI: python -m sitt.synth --weeks 16 [--start 2026-06-01] [--seed 1]

Builds data/synthetic.duckdb (the real timetable plus invented observations) and the
Parquet batches under data/synthetic/. It never writes to the real database.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from sitt.config import load_settings
from sitt.synth.generate import SynthConfig, SynthError, build_database

DEFAULT_SYNTHETIC_DB = Path("data/synthetic.duckdb")
DEFAULT_OUT_DIR = Path("data/synthetic")
DEFAULT_START = date(2026, 6, 1)  # a Monday, at the start of the monsoon


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sitt.synth",
        description="Generate SYNTHETIC observations for the loaded timetable, into a separate "
        "database. The data is invented and says nothing about real trains.",
    )
    parser.add_argument(
        "--weeks", type=int, default=16, help="how many weeks to invent (default 16)"
    )
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=DEFAULT_START,
        help=f"first day, YYYY-MM-DD (default {DEFAULT_START})",
    )
    parser.add_argument("--seed", type=int, default=1, help="random seed (default 1)")
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_SYNTHETIC_DB,
        help=f"database to build; replaced if it exists (default {DEFAULT_SYNTHETIC_DB})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"folder for the Parquet batches (default {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--timetable-db",
        type=Path,
        help="database to copy the timetable from (default: SITT_DB_PATH or data/sitt.duckdb)",
    )
    parser.add_argument(
        "--long-distance",
        type=int,
        nargs="?",
        const=24,
        default=0,
        metavar="TRAINS",
        help="also invent this many long-distance trains a day at Kalyan (24 if no number "
        "is given), to exercise the long-distance feature. Off by default",
    )
    args = parser.parse_args(argv)
    if args.weeks < 1:
        parser.error("--weeks must be at least 1")

    timetable_db = args.timetable_db or load_settings().db_path
    try:
        config = SynthConfig(long_distance_trains=args.long_distance)
        result = build_database(
            timetable_db, args.db, args.out, args.start, args.weeks, args.seed, config
        )
    except SynthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"SYNTHETIC data written to {result.db_path} (seed {result.seed}).\n"
        f"  {result.days} days from {result.start}: {result.runs} train runs, "
        f"{result.cancelled_runs} cancelled, {result.blocks} megablocks\n"
        f"  {result.observations} observations in {result.batches} batches under "
        f"{result.observations_dir}\n"
        "These numbers are invented. They say nothing about real trains."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
