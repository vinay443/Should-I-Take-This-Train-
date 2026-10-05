"""Render a saved model's metadata as a Markdown results page."""

SEGMENT_TITLES = {
    "all": "All test rows",
    "no_live_reading": "Before the train has been seen (no live reading yet)",
    "live_reading": "With an earlier live reading of the same trip",
}
PREDICTOR_TITLES = {
    "model": "LightGBM model",
    "train_station": "Baseline: median by train and station",
    "hour_weekday": "Baseline: median by hour, weekday and direction",
    "zero": "Baseline: always on time",
}
SYNTHETIC_HEADING = "SYNTHETIC RESULTS: these say nothing about real trains"


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_report(metadata: dict) -> str:
    synthetic = metadata.get("synthetic")
    lines = []
    if synthetic:
        lines += [
            f"# Delay model results ({SYNTHETIC_HEADING})",
            "",
            "**The model below was trained and tested on invented data** from `sitt.synth`",
            "(see [`synthetic-data.md`](synthetic-data.md)). The numbers show that the",
            "pipeline runs end to end and that the model can find patterns the generator put",
            "there. They are **not** an estimate of how well any of this predicts real trains.",
            "Real accuracy can only be measured once real observations have been collected.",
        ]
    else:
        lines += ["# Delay model results", "", "Trained and tested on collected observations."]
    periods, rows = metadata["periods"], metadata["rows"]
    lines += [
        "",
        f"Generated from `{metadata.get('trained_on') or 'unknown'}` on "
        f"{metadata['created_at'][:10]} by `python -m sitt.models train`.",
        "",
        "## Data and split",
        "",
        "The split is by time only: the model never trains on days after the ones it is",
        "tested on, and rows are never shuffled between periods.",
        "",
        "| Period | Days | Rows |",
        "| --- | --- | --- |",
    ]
    for name in ("train", "validation", "test"):
        first, last = periods[name]
        lines.append(f"| {name.capitalize()} | {first} to {last} | {rows[name]:,} |")
    lines += [
        "",
        f"Observation sources: {', '.join(f'`{s}`' for s in metadata['sources'])}.",
        "A row is one question: *given what was known at some earlier moment, how late was",
        "this train at this station?* One observation gives several rows: one per earlier",
        "reading of the same trip, and one from before the train started. See",
        "`src/sitt/models/features.py`.",
        "",
        "## Accuracy on the test period",
        "",
        'MAE is the mean absolute error in minutes. "Within 2 min" is the share of',
        "predictions that, rounded to a whole minute, are no more than 2 minutes from the",
        "delay that was observed. (Sources report delays in whole minutes.)",
    ]
    for segment, title in SEGMENT_TITLES.items():
        results = metadata["metrics"].get(segment, {})
        if not results.get("model", {}).get("rows"):
            continue
        lines += [
            "",
            f"### {title}",
            "",
            f"{results['model']['rows']:,} rows.",
            "",
            "| Predictor | MAE (min) | Within 2 min | Within 5 min |",
            "| --- | --- | --- | --- |",
        ]
        for name, label in PREDICTOR_TITLES.items():
            m = results[name]
            lines.append(
                f"| {label} | {m['mae']:.2f} | {_percent(m['within_2'])} | "
                f"{_percent(m['within_5'])} |"
            )
    lines += [
        "",
        "## Calibration of the 10th–90th percentile range",
        "",
        "A well-calibrated range holds the observed delay 80% of the time, with 10% below",
        "and 10% above.",
        "",
        "| Rows | Inside the range | Below | Above | Mean width (min) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for segment, title in SEGMENT_TITLES.items():
        m = metadata["metrics"].get(segment, {}).get("model", {})
        if m.get("rows"):
            lines.append(
                f"| {title} | {_percent(m['range_coverage'])} | {_percent(m['below_range'])} | "
                f"{_percent(m['above_range'])} | {m['range_width']:.1f} |"
            )
    lines += [
        "",
        "## What the model leans on",
        "",
        "Share of the median model's total split gain, top ten features.",
        "",
        "| Feature | Share of gain |",
        "| --- | --- |",
    ]
    lines += [f"| `{name}` | {_percent(share)} |" for name, share in metadata["importance"][:10]]
    trees = metadata["trees"]
    lines += [
        "",
        "## Settings",
        "",
        f"- Trees after early stopping: median {trees['point']}, 10th percentile "
        f"{trees['q10']}, 90th percentile {trees['q90']}.",
        f"- Seed {metadata['config']['seed']}; LightGBM parameters are in "
        "`src/sitt/models/delay.py`.",
    ]
    if synthetic:
        lines += [
            "",
            "## How to read this",
            "",
            "- The model beating the baselines here means the features carry the signal the",
            "  generator planted (delay building along a route, the state of the line, peaks,",
            "  megablocks). It does not mean real delays follow those patterns.",
            "- The synthetic delays are smoother and more regular than real ones are likely to",
            "  be, so real errors will probably be larger.",
            "- When real observations exist, run the same command on the real database and",
            "  replace this page.",
        ]
    return "\n".join(lines) + "\n"
