"""CLI: python -m sitt.ingest.live [--source mobond|ntes|all] [--dry-run]"""

import argparse
import logging
import os
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from sitt.ingest.live.collect import SOURCES, CollectResult, collect
from sitt.ingest.live.common import make_client


def summarise(result: CollectResult, dry_run: bool) -> str:
    lines = [f"Batch {result.batch_id}{' (dry run, nothing written)' if dry_run else ''}"]
    for source in result.sources:
        if not source.ok:
            lines.append(f"  {source.source}: FAILED: {source.error}")
            continue
        events = Counter(o.event for o in source.observations)
        delays = [o.delay_minutes for o in source.observations if o.delay_minutes is not None]
        median = sorted(delays)[len(delays) // 2] if delays else None
        lines.append(
            f"  {source.source}: {len(source.observations)} observations "
            f"({', '.join(f'{k} {v}' for k, v in events.most_common())}); "
            f"cancelled {sum(o.cancelled for o in source.observations)}; "
            f"median delay {median if median is not None else 'n/a'} min"
        )
        if source.raw_path:
            lines.append(f"    raw: {source.raw_path}")
        if dry_run:
            lines += [
                f"    {o.train_number} {o.event} {o.station_code or '-'} "
                f"delay={o.delay_minutes} kind={o.time_kind} | {o.raw_status}"
                for o in source.observations[:5]
            ]
    if result.parquet_path:
        lines.append(f"  parquet: {result.parquet_path}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sitt.ingest.live",
        description="Fetch live running status once from each source and store it.",
    )
    parser.add_argument("--source", choices=[*SOURCES, "all"], default="all")
    parser.add_argument(
        "--dry-run", action="store_true", help="fetch and parse, print a summary, write nothing"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="writes DATA_DIR/observations/ and DATA_DIR/raw/ (default: data)",
    )
    parser.add_argument("--raw-dir", type=Path, help="override where raw responses go")
    parser.add_argument(
        "--load",
        action="store_true",
        help="afterwards, import new batches into the local DuckDB (see sitt.ingest.live.load)",
    )
    args = parser.parse_args(argv)

    load_dotenv(find_dotenv(usecwd=True))
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=os.environ.get("SITT_LOG_LEVEL") or "INFO",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    sources = list(SOURCES) if args.source == "all" else [args.source]
    observations_dir = args.data_dir / "observations"
    with make_client() as client:
        result = collect(
            sources,
            client,
            observations_dir=observations_dir,
            raw_dir=args.raw_dir or args.data_dir / "raw",
            dry_run=args.dry_run,
        )
    print(summarise(result, args.dry_run))

    if args.load and not args.dry_run:
        from sitt.ingest.live.load import main as load_main

        load_main(["--dir", str(observations_dir)])
    # Non-zero if any source failed, so a scheduled run shows up red, even though the
    # sources that worked have still been written.
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
