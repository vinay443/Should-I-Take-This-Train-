"""CLI for the delay model.

    python -m sitt.models train  [--db data/synthetic.duckdb] [--out models/delay]
    python -m sitt.models report [--model models/delay] [--write docs/model-results.md]

`train` builds features, trains on the earlier weeks, evaluates on the last ones against
the baselines, and saves the model with a metadata file. `report` prints those results
as Markdown.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import duckdb

from sitt.models.delay import DEFAULT_MODEL_DIR, DelayModel, ModelError, TrainConfig, train
from sitt.models.report import PREDICTOR_TITLES, SYNTHETIC_HEADING, render_report

DEFAULT_TRAINING_DB = Path("data/synthetic.duckdb")


def _summary(model: DelayModel) -> str:
    metadata = model.metadata
    lines = []
    if model.synthetic:
        lines.append(SYNTHETIC_HEADING.upper())
    periods = metadata["periods"]
    lines.append(
        f"Trained on {periods['train'][0]} to {periods['train'][1]} "
        f"({metadata['rows']['train']:,} rows), tested on {periods['test'][0]} to "
        f"{periods['test'][1]} ({metadata['rows']['test']:,} rows)."
    )
    results = metadata["metrics"]["all"]
    for name, label in PREDICTOR_TITLES.items():
        m = results[name]
        lines.append(
            f"  {label:<50} MAE {m['mae']:.2f}  within 2 min {m['within_2'] * 100:.0f}%  "
            f"within 5 min {m['within_5'] * 100:.0f}%"
        )
    m = results["model"]
    lines.append(
        f"  10th-90th percentile range holds {m['range_coverage'] * 100:.0f}% of test rows "
        f"(aim: 80%), mean width {m['range_width']:.1f} min"
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sitt.models", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    train_parser = commands.add_parser("train", help="train, evaluate and save the delay model")
    train_parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_TRAINING_DB,
        help=f"database with observations and the timetable (default {DEFAULT_TRAINING_DB})",
    )
    train_parser.add_argument("--out", type=Path, default=DEFAULT_MODEL_DIR)
    train_parser.add_argument("--test-weeks", type=int, default=TrainConfig.test_weeks)
    train_parser.add_argument("--valid-weeks", type=int, default=TrainConfig.valid_weeks)
    train_parser.add_argument("--rounds", type=int, default=TrainConfig.rounds)
    train_parser.add_argument("--seed", type=int, default=TrainConfig.seed)
    train_parser.add_argument(
        "--max-train-rows", type=int, help="sample the training rows down to this many"
    )

    report_parser = commands.add_parser("report", help="print a saved model's results as Markdown")
    report_parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_DIR)
    report_parser.add_argument("--write", type=Path, help="write to this file instead of printing")

    args = parser.parse_args(argv)
    try:
        if args.command == "train":
            if not args.db.exists():
                raise ModelError(f"no database at {args.db}")
            config = TrainConfig(
                test_weeks=args.test_weeks,
                valid_weeks=args.valid_weeks,
                rounds=args.rounds,
                seed=args.seed,
                max_train_rows=args.max_train_rows,
            )
            with duckdb.connect(str(args.db), read_only=True) as con:
                model = train(con, config, source_label=args.db.as_posix())
            model.save(args.out)
            print(_summary(model))
            print(f"Saved to {args.out}")
        else:
            text = render_report(DelayModel.load(args.model).metadata)
            if args.write:
                # The page's real-data section belongs to sitt-retrain: carry it over.
                from sitt.models.retrain import extract_real_section

                kept = None
                if args.write.is_file():
                    kept = extract_real_section(args.write.read_text(encoding="utf-8"))
                if kept:
                    text = text.rstrip("\n") + "\n\n" + kept + "\n"
                args.write.write_text(text, encoding="utf-8", newline="\n")
                print(f"Wrote {args.write}")
            else:
                print(text)
    except ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
