"""Streamlit dashboard. Run with `uv run sitt-dashboard` or
`uv run streamlit run src/sitt/dashboard/app.py`. See docs/dashboard.md.
"""

from datetime import datetime, time
from pathlib import Path

import altair as alt
import duckdb
import pandas as pd
import streamlit as st

from sitt.bot.formatting import trip_tags
from sitt.config import load_settings
from sitt.dashboard import data
from sitt.models.crowding import CROWD_LABELS
from sitt.models.report import PREDICTOR_TITLES, SEGMENT_TITLES
from sitt.recommend import explain, recommend
from sitt.timetable import next_trains
from sitt.tz import IST

PAGES = ("Timetable", "Delay patterns", "Crowd reports", "Recommender", "Model metrics")

st.set_page_config(page_title="Should I take this train?", page_icon="🚆", layout="wide")


@st.cache_resource(show_spinner="Getting the data ready (first run can take a minute)…")
def get_sources() -> data.Sources:
    return data.find_sources()


def _stamp(db: Path | None) -> float:
    """Changes when the database file does, so cached queries refresh."""
    return db.stat().st_mtime if db and db.exists() else 0.0


@st.cache_data(show_spinner=False)
def cached(name: str, db: str, stamp: float, *args):
    return getattr(data, name)(Path(db), *args)


def query(name: str, db: Path, *args):
    return cached(name, str(db), _stamp(db), *args)


def banners(sources: data.Sources, observations: bool) -> None:
    if sources.timetable_kind == data.TIMETABLE_SAMPLE:
        st.error(data.SAMPLE_BANNER, icon="⚠️")
    for note in sources.notes:
        st.caption(note)
    if observations and sources.synthetic:
        st.warning(data.SYNTHETIC_BANNER, icon="⚠️")


def station_pickers(sources: data.Sources, key: str) -> tuple[str, str]:
    frame = query("stations", sources.timetable_db)
    labels = {f"{row.name} ({row.code})": row.code for row in frame.itertuples()}
    names = list(labels)
    codes = list(labels.values())
    start = codes.index("KYN") if "KYN" in codes else 0
    end = codes.index("CSMT") if "CSMT" in codes else len(codes) - 1
    left, right = st.columns(2)
    origin = left.selectbox("From", names, index=start, key=f"{key}-from")
    destination = right.selectbox("To", names, index=end, key=f"{key}-to")
    return labels[origin], labels[destination]


def moment_picker(sources: data.Sources, key: str) -> datetime:
    default = data.default_moment(sources, datetime.now(IST).replace(tzinfo=None))
    left, right = st.columns(2)
    day = left.date_input("Date", default.date(), key=f"{key}-date")
    at = right.time_input("Time", default.time(), key=f"{key}-time", step=300)
    return datetime.combine(day, at if isinstance(at, time) else default.time())


def heatmap(frame: pd.DataFrame, x: str, y: str, y_sort: list | None, title: str) -> alt.Chart:
    rows = frame[y].nunique()
    return (
        alt.Chart(frame, title=title, height=max(160, 16 * rows))
        .mark_rect()
        .encode(
            x=alt.X(f"{x}:O", title="Hour of day"),
            y=alt.Y(f"{y}:N", sort=y_sort, title=None),
            color=alt.Color(
                "delay:Q", title="Mean delay (min)", scale=alt.Scale(scheme="orangered")
            ),
            tooltip=[y, x, alt.Tooltip("delay:Q", format=".1f"), "readings"],
        )
    )


# --- Pages --------------------------------------------------------------------------------


def timetable_page(sources: data.Sources) -> None:
    st.header("Timetable explorer")
    banners(sources, observations=False)
    summary = query("timetable_summary", sources.timetable_db)
    columns = st.columns(6)
    for column, (label, key) in zip(
        columns,
        [
            ("Trains", "trains"),
            ("Stations", "stations"),
            ("Fast", "fast"),
            ("AC", "ac"),
            ("15-car", "fifteen_car"),
            ("Ladies' specials", "ladies_special"),
        ],
        strict=True,
    ):
        column.metric(label, summary[key])

    origin, destination = station_pickers(sources, "timetable")
    when = moment_picker(sources, "timetable")
    count = st.slider("How many trains", 5, 40, 15)
    if origin == destination:
        st.info("Pick two different stations.")
        return
    with duckdb.connect(str(sources.timetable_db), read_only=True) as con:
        trips = next_trains(con, origin, destination, when, n=count)
    if not trips:
        st.info("No trains between those stations in the next day.")
        return
    st.dataframe(
        pd.DataFrame(
            {
                "Departs": [f"{t.departure:%a %H:%M}" for t in trips],
                "Arrives": [f"{t.arrival:%H:%M}" for t in trips],
                "Minutes": [round(t.duration.total_seconds() / 60) for t in trips],
                "Type": [t.train_type for t in trips],
                "To": [t.label for t in trips],
                "Tags": [" · ".join(trip_tags(t)) for t in trips],
                "Code": [t.service_code or "" for t in trips],
                "Number": [t.number for t in trips],
            }
        ),
        hide_index=True,
        width="stretch",
    )
    st.caption("Scheduled times. AC tags allow for trains that run without AC at weekends.")


def delays_page(sources: data.Sources) -> None:
    st.header("Delay patterns")
    banners(sources, observations=True)
    db = sources.observations_db
    if db is None:
        st.info("No observations yet.")
        return
    summary = query("observation_summary", db)
    st.caption(
        f"{summary['rows']:,} readings from {summary['first']:%d %b %Y} to "
        f"{summary['last']:%d %b %Y}, sources: {', '.join(summary['sources'])}. "
        f"{summary['cancelled']:,} are cancellation reports."
    )

    direction = st.radio(
        "Direction", ["up", "down"], horizontal=True,
        format_func=lambda d: "Up (towards CSMT)" if d == "up" else "Down (away from CSMT)",
    )  # fmt: skip
    by_hour = query("delay_by_hour_weekday", db, direction)
    st.altair_chart(
        heatmap(by_hour, "hour", "weekday", data.WEEKDAYS, "Mean delay by hour and weekday"),
        width="stretch",
    )
    by_station = query("delay_by_station", db, direction)
    order = list(by_station.sort_values("seq")["station"].unique())
    if direction == "up":
        order.reverse()  # in the order the train reaches them
    st.altair_chart(
        heatmap(by_station, "hour", "station", order, "Mean delay by station along the route"),
        width="stretch",
    )

    st.subheader("Per train")
    trains = query("delay_by_train", db)
    st.dataframe(
        trains.rename(
            columns={
                "number": "Number",
                "service_code": "Code",
                "train_type": "Type",
                "direction": "Direction",
                "destination": "To",
                "departs": "Departs",
                "median_delay": "Median delay",
                "p90_delay": "Bad-day delay (90th pct)",
                "worst_delay": "Worst",
                "days": "Days seen",
                "readings": "Readings",
                "cancelled_days": "Days cancelled",
            }
        ),
        hide_index=True,
        width="stretch",
        height=320,
    )
    numbers = list(trains["number"])
    if numbers:
        chosen = st.selectbox("Delay along one train's route", numbers)
        route = query("delay_along_route", db, chosen)
        melted = route.melt(
            id_vars=["station", "point_seq"],
            value_vars=["median_delay", "p90_delay"],
            var_name="measure",
            value_name="minutes",
        )
        melted["measure"] = melted["measure"].map(
            {"median_delay": "Median", "p90_delay": "90th percentile"}
        )
        st.altair_chart(
            alt.Chart(melted)
            .mark_line(point=True)
            .encode(
                x=alt.X("station:N", sort=list(route["station"]), title=None),
                y=alt.Y("minutes:Q", title="Delay (min)"),
                color=alt.Color("measure:N", title=None),
                tooltip=["station", "measure", alt.Tooltip("minutes:Q", format=".1f")],
            ),
            width="stretch",
        )


def crowd_page(sources: data.Sources) -> None:
    st.header("Crowd reports")
    st.caption(
        "Reports logged through the Telegram bot with /log. These are real, never synthetic."
    )
    reports = query("crowd_reports", sources.crowd_db)
    if reports.empty:
        st.info(
            "No crowd reports yet. Log one with the bot: `/log 8:12 fast KYN packed` "
            "(see docs/bot-setup.md)."
        )
        return
    reports["level"] = reports["crowd_level"].map(lambda v: f"{v} {CROWD_LABELS[int(v)]}")
    left, right = st.columns(2)
    left.metric("Reports", len(reports))
    right.metric("Mean level", f"{reports['crowd_level'].mean():.1f} / 5")
    st.altair_chart(
        alt.Chart(reports, title="Reports by crowd level")
        .mark_bar()
        .encode(x=alt.X("level:N", title=None), y=alt.Y("count():Q", title="Reports")),
        width="stretch",
    )
    reports["hour"] = reports["reported"].dt.hour
    st.altair_chart(
        alt.Chart(reports, title="Mean crowd level by hour reported")
        .mark_bar()
        .encode(
            x=alt.X("hour:O", title="Hour"), y=alt.Y("mean(crowd_level):Q", title="Mean level")
        ),
        width="stretch",
    )
    st.dataframe(
        reports[["reported", "station_code", "train", "level", "note"]].rename(
            columns={
                "reported": "Reported",
                "station_code": "Station",
                "train": "Train",
                "level": "Crowd",
                "note": "Message",
            }
        ),  # fmt: skip
        hide_index=True,
        width="stretch",
    )


def recommender_page(sources: data.Sources) -> None:
    st.header("Take this train, or wait?")
    banners(sources, observations=False)
    settings = load_settings()
    origin, destination = station_pickers(sources, "recommend")
    when = moment_picker(sources, "recommend")
    left, right = st.columns(2)
    use_model = left.checkbox("Use the trained delay model, if there is one", value=True)
    use_history = right.checkbox(
        "Use observations for past delays", value=sources.timetable_db == sources.observations_db,
        help="Reads the observations shown on the Delay patterns page. When those are synthetic, "
        "so is everything predicted from them.",
    )  # fmt: skip
    if origin == destination:
        st.info("Pick two different stations.")
        return
    db = (
        sources.observations_db if use_history and sources.observations_db else sources.timetable_db
    )
    with duckdb.connect(str(db), read_only=True) as con:
        result = recommend(
            con, origin, destination, when, settings.recommend,
            model_dir=settings.model_dir if use_model else None,
        )  # fmt: skip
    if result.synthetic:
        st.warning(
            "SYNTHETIC predictions. The delay figures below come from invented data and say "
            "nothing about real trains. Only the timetable times are real.",
            icon="⚠️",
        )
    st.subheader(result.reason)
    st.caption(f"Predictions used: {result.level_text}. Crowding is a rule-of-thumb estimate.")
    rows = []
    for i, option in enumerate(result.options):
        trip = option.trip
        rows.append(
            {
                "": "➜" if i == result.choice else "",
                "Departs": f"{trip.departure:%a %H:%M}",
                "Train": f"{trip.train_type} to {trip.label}",
                "Scheduled arrival": f"{trip.arrival:%H:%M}",
                "Predicted arrival": "cancelled"
                if option.cancelled
                else f"{option.predicted_arrival:%H:%M}",
                "± min": option.arrival_margin,
                "Crowding": f"{option.crowding.score} {option.crowding.label}"
                if option.crowding
                else "",
                "Why": option.crowding.reason if option.crowding else "",
                "Tags": " · ".join(trip_tags(trip)),
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    with st.expander("Why this recommendation?"):
        st.text(explain(result))


def metrics_table(results: dict) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Predictor": title,
                "MAE (min)": round(results[name]["mae"], 2),
                "Within 2 min": f"{results[name]['within_2'] * 100:.1f}%",
                "Within 5 min": f"{results[name]['within_5'] * 100:.1f}%",
            }
            for name, title in PREDICTOR_TITLES.items()
            if results.get(name, {}).get("rows")
        ]
    )


def model_page(sources: data.Sources) -> None:
    st.header("Model metrics")
    metadata = data.load_model_metadata(load_settings().model_dir)
    if metadata is None:
        st.info(
            "No trained model in this deployment, so this is the last results page written "
            "to the repository (docs/model-results.md)."
        )
        if data.MODEL_RESULTS_DOC.exists():
            text = data.MODEL_RESULTS_DOC.read_text(encoding="utf-8")
            if "SYNTHETIC" in text:
                st.warning(
                    "SYNTHETIC RESULTS. These numbers come from invented data and say nothing "
                    "about real trains.",
                    icon="⚠️",
                )
            st.markdown(text)
        return
    if metadata.get("synthetic"):
        st.warning(
            "SYNTHETIC RESULTS. This model was trained and tested on invented data. The numbers "
            "show the pipeline works. They say nothing about real trains.",
            icon="⚠️",
        )
    periods = metadata["periods"]
    st.caption(
        f"Trained {metadata['created_at'][:10]} on `{metadata.get('trained_on') or 'unknown'}`. "
        f"Train {periods['train'][0]} to {periods['train'][1]}, test {periods['test'][0]} to "
        f"{periods['test'][1]}. The split is by time; rows are never shuffled between periods."
    )
    for segment, title in SEGMENT_TITLES.items():
        results = metadata["metrics"].get(segment, {})
        if results.get("model", {}).get("rows"):
            st.subheader(title)
            st.caption(f"{results['model']['rows']:,} test rows")
            st.dataframe(metrics_table(results), hide_index=True, width="stretch")
    overall = metadata["metrics"]["all"]["model"]
    st.subheader("10th–90th percentile range")
    left, middle, right = st.columns(3)
    left.metric(
        "Holds the observed delay", f"{overall['range_coverage'] * 100:.1f}%", help="Aim: 80%"
    )
    middle.metric("Mean width", f"{overall['range_width']:.1f} min")
    right.metric(
        "Below / above",
        f"{overall['below_range'] * 100:.0f}% / {overall['above_range'] * 100:.0f}%",
    )
    st.subheader("What the model leans on")
    importance = pd.DataFrame(metadata["importance"][:12], columns=["feature", "share"])
    st.altair_chart(
        alt.Chart(importance)
        .mark_bar()
        .encode(
            x=alt.X("share:Q", title="Share of split gain", axis=alt.Axis(format="%")),
            y=alt.Y("feature:N", sort="-x", title=None),
        ),
        width="stretch",
    )


def main() -> None:
    sources = get_sources()
    st.sidebar.title("Should I take this train?")
    page = st.sidebar.radio("Page", PAGES, label_visibility="collapsed")
    kinds = {
        data.TIMETABLE_REAL: "the loaded timetable",
        data.TIMETABLE_DOWNLOADED: "Central Railway's PDFs, downloaded for this demo",
        data.TIMETABLE_SAMPLE: "an invented sample (not real trains)",
    }
    st.sidebar.caption(f"Timetable: {kinds[sources.timetable_kind]}.")
    st.sidebar.caption(
        "Observations: " + ("SYNTHETIC (invented)." if sources.synthetic else "collected.")
    )
    {
        "Timetable": timetable_page,
        "Delay patterns": delays_page,
        "Crowd reports": crowd_page,
        "Recommender": recommender_page,
        "Model metrics": model_page,
    }[page](sources)


main()
