"""CLI for the delay model.

    python -m sitt.models train  [--db data/synthetic.duckdb] [--out models/delay]
    python -m sitt.models report [--model models/delay] [--write docs/model-results.md]
    python -m sitt.models ablate [--db data/synthetic.duckdb] [--feature long_distance]

`train` builds features, trains on the earlier weeks, evaluates on the last ones against
the baselines, and saves the model with a metadata file. `report` prints those results
as Markdown. `ablate` trains the model twice, with and without an experimental group of
features, and prints both sets of test results side by side. It saves nothing.
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import duckdb

from sitt.config import long_distance_feature_enabled
from sitt.models.delay import DEFAULT_MODEL_DIR, DelayModel, ModelError, TrainConfig, train
from sitt.models.features import LONG_DISTANCE_FEATURES
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


# Experimental feature groups that can be compared with `ablate`.
ABLATIONS: dict[str, tuple[str, ...]] = {"long_distance": LONG_DISTANCE_FEATURES}


def ablate(
    con: duckdb.DuckDBPyConnection, config: TrainConfig, feature: str, source_label: str = ""
) -> dict:
    """Train with and without one experimental feature group; return both test results.

    Same data, same split and same seed for both, so the only difference is the inputs.
    """
    extra = ABLATIONS[feature]
    without = train(con, replace(config, extra_features=()), source_label)
    with_extra = train(con, replace(config, extra_features=extra), source_label)
    importance = dict(with_extra.metadata["importance"])
    labelled = con.execute("SELECT count(*) FROM feature_rows WHERE ld_count > 0").fetchone()[0]
    return {
        "feature": feature,
        "extra_features": list(extra),
        "synthetic": with_extra.synthetic,
        "trained_on": source_label,
        "periods": with_extra.metadata["periods"],
        "rows": with_extra.metadata["rows"],
        "rows_with_the_feature": labelled,
        "without": without.metadata["metrics"]["all"]["model"],
        "with": with_extra.metadata["metrics"]["all"]["model"],
        "importance": {name: importance.get(name, 0.0) for name in extra},
    }


def render_ablation(result: dict) -> str:
    lines = []
    if result["synthetic"]:
        lines.append(SYNTHETIC_HEADING.upper())
    periods, rows = result["periods"], result["rows"]
    lines += [
        f"Feature group: {result['feature']} ({', '.join(result['extra_features'])})",
        f"Data: {result['trained_on'] or 'unknown'}; tested on {periods['test'][0]} to "
        f"{periods['test'][1]} ({rows['test']:,} rows; {rows['train']:,} trained on)",
        f"Rows where the feature has a value: {result['rows_with_the_feature']:,}",
        "",
        f"{'':<22}{'MAE':>8}{'within 2':>10}{'within 5':>10}{'coverage':>10}{'width':>8}",
    ]
    for label, key in (("without the feature", "without"), ("with the feature", "with")):
        m = result[key]
        lines.append(
            f"{label:<22}{m['mae']:>8.3f}{m['within_2'] * 100:>9.1f}%{m['within_5'] * 100:>9.1f}%"
            f"{m['range_coverage'] * 100:>9.1f}%{m['range_width']:>8.2f}"
        )
    change = result["with"]["mae"] - result["without"]["mae"]
    shares = ", ".join(f"{name} {share * 100:.1f}%" for name, share in result["importance"].items())
    lines += [
        "",
        f"MAE change with the feature: {change:+.3f} min. Share of the model's gain: {shares}.",
        "One run on one split. A difference this size can be noise; it is not a finding.",
    ]
    if result["synthetic"]:
        lines.append(
            "On synthetic data this only shows that the feature is computed and reaches the "
            "model. It says nothing about real trains."
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
    train_parser.add_argument(
        "--long-distance-feature",
        action="store_true",
        help="also train on the long-distance congestion inputs (an experiment; default: "
        "SITT_FEATURE_LONG_DISTANCE)",
    )

    ablate_parser = commands.add_parser(
        "ablate", help="compare test results with and without an experimental feature group"
    )
    ablate_parser.add_argument("--db", type=Path, default=DEFAULT_TRAINING_DB)
    ablate_parser.add_argument("--feature", choices=sorted(ABLATIONS), default="long_distance")
    ablate_parser.add_argument("--test-weeks", type=int, default=TrainConfig.test_weeks)
    ablate_parser.add_argument("--valid-weeks", type=int, default=TrainConfig.valid_weeks)
    ablate_parser.add_argument("--seed", type=int, default=TrainConfig.seed)
    ablate_parser.add_argument("--max-train-rows", type=int)

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
                extra_features=(
                    LONG_DISTANCE_FEATURES
                    if args.long_distance_feature or long_distance_feature_enabled()
                    else ()
                ),
            )
            with duckdb.connect(str(args.db), read_only=True) as con:
                model = train(con, config, source_label=args.db.as_posix())
            model.save(args.out)
            print(_summary(model))
            print(f"Saved to {args.out}")
        elif args.command == "ablate":
            if not args.db.exists():
                raise ModelError(f"no database at {args.db}")
            config = TrainConfig(
                test_weeks=args.test_weeks,
                valid_weeks=args.valid_weeks,
                seed=args.seed,
                max_train_rows=args.max_train_rows,
            )
            with duckdb.connect(str(args.db), read_only=True) as con:
                print(render_ablation(ablate(con, config, args.feature, args.db.as_posix())))
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
