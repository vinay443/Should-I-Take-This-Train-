"""Mobond (m-Indicator) live-train feed: one GET returning `{train_no: status text}`.

This is an undocumented endpoint of a commercial app (docs/data-sources.md §2.1), so the
adapter is **off, and stays off until Mobond has agreed to being polled.** It is built to
be switched on carefully when that happens:

* **Double opt-in.** `fetch` makes no request unless two separate settings are both
  exactly `true`: `SITT_MOBOND_ENABLED` (you want it on) and
  `SITT_MOBOND_PERMISSION_CONFIRMED` (Mobond has said yes). Either alone does nothing.
  The check is inside `fetch` itself, so no caller can get round it by accident.
* **Rate limit.** At most one request per `SITT_MOBOND_MIN_INTERVAL_MINUTES`, never less
  than 15, however often the collector is run. The time of the last request is kept in a
  small state file (`data/logs/mobond_state.json`), and it is written *before* the
  request goes out, so a crash can't turn into a tight loop.
* **Backoff.** Each failed request doubles the wait before the next one, up to a day.
  One success resets it.
* **Timeouts** shorter than the other sources', and a User-Agent that says who is asking,
  how often, and where the code is.

A request that isn't made for any of these reasons raises `MobondSkipped`, which the
collector records as "not polled this run", not as a failure.

`parse_response` turns the feed into `Observation`s; see `parse_status` for the mapping
into the `observations` columns.
"""

import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from sitt.ingest.live.common import Observation, RawResponse, SourceError, request
from sitt.ingest.live.stations import resolve_station

SOURCE = "mobond"
URL = "https://mobond.com/mtracker/getalllivetrains"

ENABLE_VARIABLE = "SITT_MOBOND_ENABLED"
PERMISSION_VARIABLE = "SITT_MOBOND_PERMISSION_CONFIRMED"
MIN_INTERVAL_VARIABLE = "SITT_MOBOND_MIN_INTERVAL_MINUTES"
MIN_INTERVAL_FLOOR_MINUTES = 15.0  # docs/data-sources.md: at most one request per 15-30 min
MAX_BACKOFF = timedelta(hours=24)
# Task Scheduler starts a run a few seconds either side of the quarter hour. Without this
# allowance a run starting 2 seconds "early" would be skipped and the next one accepted,
# halving the polling rate for no reason.
SCHEDULE_SLACK = timedelta(seconds=60)

STATE_FILE = "mobond_state.json"
DEFAULT_STATE_FILE = Path("data") / "logs" / STATE_FILE
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
PROJECT_URL = "https://github.com/vinay443/Should-I-Take-This-Train-"

# Every shape seen in three snapshots on 2026-09-27, e.g.
#   "[7 min ago] Between AMBARNATH - BADLAPUR, 36 min Late (Less Accurate)"
#   "Reaching CSMT (at VIDYAVIHAR now), 39 min Late"
#   "Cancellation Reported"
_STATUS_RE = re.compile(
    r"""
    ^(?:\[(?P<ago>\d+)\ min\ ago\]\ )?
    (?:
        (?P<cancelled>Cancellation\ Reported)
      | Rake\ at\ (?P<rake_at>[^,(]+?)
      | At\ (?P<at>[^,(]+?)
      | Crossed\ (?P<crossed>[^,(]+?)
      | Arriving\ (?P<arriving>[^,(]+?)
      | Between\ (?P<between>[^,(]+?)\ -\ (?P<between_next>[^,(]+?)
      | Reaching\ (?P<reaching>[^,(]+?)\ \(at\ (?P<reaching_now>[^)]+?)\ now\)
    )
    (?:,\ (?P<delay>\d+)\ min\ (?P<direction>Late|Early))?
    (?P<less_accurate>\ \(Less\ Accurate\))?$
    """,
    re.VERBOSE,
)


class MobondSkipped(SourceError):
    """No request was made this time, on purpose. Not a failure of the source."""


class MobondDisabledError(MobondSkipped):
    """The double opt-in isn't complete, so no request may be made at all."""


# --- the double opt-in ---


def _is_true(environ: Mapping[str, str], name: str) -> bool:
    return environ.get(name, "").strip().lower() == "true"


def opt_in(environ: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """(whether Mobond may be polled, why not). Both switches must be exactly `true`."""
    environ = os.environ if environ is None else environ
    enabled = _is_true(environ, ENABLE_VARIABLE)
    permitted = _is_true(environ, PERMISSION_VARIABLE)
    if enabled and permitted:
        return True, ""
    if enabled:
        return False, (
            f"Mobond is off: {ENABLE_VARIABLE} is set but {PERMISSION_VARIABLE} is not. "
            "Set it to true only once Mobond has agreed to being polled"
        )
    if permitted:
        return False, f"Mobond is off: {PERMISSION_VARIABLE} is set but {ENABLE_VARIABLE} is not"
    return False, (
        f"Mobond is off (it needs both {ENABLE_VARIABLE}=true and "
        f"{PERMISSION_VARIABLE}=true; see docs/data-sources.md)"
    )


def min_interval(environ: Mapping[str, str] | None = None) -> timedelta:
    """The shortest allowed gap between requests. Never below the 15-minute floor."""
    environ = os.environ if environ is None else environ
    try:
        minutes = float(environ.get(MIN_INTERVAL_VARIABLE, "") or MIN_INTERVAL_FLOOR_MINUTES)
    except ValueError:
        minutes = MIN_INTERVAL_FLOOR_MINUTES
    return timedelta(minutes=max(MIN_INTERVAL_FLOOR_MINUTES, minutes))


def mobond_user_agent(environ: Mapping[str, str] | None = None) -> str:
    """Who is asking, how often, and where the code is. `SITT_USER_AGENT` replaces it."""
    environ = os.environ if environ is None else environ
    if override := environ.get("SITT_USER_AGENT"):
        return override
    minutes = min_interval(environ).total_seconds() / 60
    return (
        "should-i-take-this-train/0.1 (personal non-commercial research project; "
        f"at most one request every {minutes:g} minutes; +{PROJECT_URL})"
    )


# --- rate limit and backoff state ---


def load_state(path: Path) -> dict | None:
    """The saved state, {} if there is none yet, or None if the file can't be read."""
    if not path.is_file():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("last_request_at"):
            datetime.fromisoformat(state["last_request_at"])
        int(state.get("failures", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    return state


def save_state(path: Path, last_request_at: datetime, failures: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {"last_request_at": last_request_at.isoformat(), "failures": failures}
    path.write_text(json.dumps(state) + "\n", encoding="utf-8")


def wait_after(failures: int, interval: timedelta) -> timedelta:
    """How long to wait after a request: the interval, doubled for each failure in a row."""
    return min(MAX_BACKOFF, interval * (2 ** min(max(0, failures), 16)))


def next_allowed(state: dict, interval: timedelta) -> datetime | None:
    """The earliest moment another request may be made, or None if there is no history."""
    if not state.get("last_request_at"):
        return None
    last = datetime.fromisoformat(state["last_request_at"])
    return last + wait_after(int(state.get("failures", 0)), interval)


def fetch(
    client: httpx.Client,
    *,
    state_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> RawResponse:
    """Fetch the feed once, if it is allowed and due. See the module docstring.

    Raises MobondDisabledError or MobondSkipped when no request is made, and SourceError
    when one is made and fails.
    """
    allowed, reason = opt_in(environ)
    if not allowed:
        raise MobondDisabledError(reason)

    state_file = Path(state_file) if state_file is not None else DEFAULT_STATE_FILE
    now = now or datetime.now(UTC)
    interval = min_interval(environ)
    state = load_state(state_file)
    if state is None:
        # Unknown history: assume a request was just made, rather than risk another.
        _record(state_file, now, 0)
        raise MobondSkipped(
            f"Mobond not polled: {state_file} could not be read, so it was reset. "
            "Polling resumes after one interval"
        )
    due = next_allowed(state, interval)
    if due is not None and now < due - SCHEDULE_SLACK:
        failures = int(state.get("failures", 0))
        why = f"backing off after {failures} failed request(s)" if failures else "rate limit"
        raise MobondSkipped(f"Mobond not polled ({why}): next request allowed at {due:%H:%M} UTC")

    failures = int(state.get("failures", 0))
    # Recorded before the request, as a failure: if this process dies mid-request, the
    # next run waits longer rather than trying again at once.
    _record(state_file, now, failures + 1)
    log: list[str] = []
    response = request(
        client,
        "GET",
        URL,
        log,
        headers={"Accept": "application/json", "User-Agent": mobond_user_agent(environ)},
        timeout=TIMEOUT,
    )
    _record(state_file, now, 0)
    return RawResponse(
        source=SOURCE,
        url=URL,
        fetched_at=now,
        status_code=response.status_code,
        content_type=response.headers.get("content-type", ""),
        body=response.text,
        requests=tuple(log),
    )


def _record(state_file: Path, when: datetime, failures: int) -> None:
    """Save the state, or refuse to go on: an unrecorded request can't be rate-limited."""
    try:
        save_state(state_file, when, failures)
    except OSError as exc:
        raise MobondSkipped(
            f"Mobond not polled: the request time can't be recorded in {state_file} ({exc})"
        ) from exc


# --- parsing ---


def parse_response(raw: RawResponse) -> list[Observation]:
    try:
        feed = json.loads(raw.body)
    except json.JSONDecodeError as exc:
        raise SourceError(f"Mobond response is not JSON: {exc}") from exc
    if not isinstance(feed, dict):
        raise SourceError(f"Mobond response is a JSON {type(feed).__name__}, expected an object")
    return [
        parse_status(str(number), str(status), raw.fetched_at) for number, status in feed.items()
    ]


def parse_status(train_number: str, status: str, fetched_at: datetime) -> Observation:
    """Turn one status string into an Observation. Unrecognised text is kept as event 'unknown'.

    How the feed maps onto the `observations` columns:

        train_number             the feed's key, as given
        observed_at              when it was fetched, minus the "[N min ago]" prefix
        event                    at | arriving | crossed | between | rake_at | cancelled |
                                 unknown ("Reaching X (at Y now)" is `at` Y)
        station_code             the station named (for `between`, the one last passed),
                                 as a code where known, else the name; '' for a cancellation
        delay_minutes            "N min Late" as +N, "N min Early" as -N, else NULL
        actual_or_expected_time  observed_at, for `at` only: the one event that pins the
        time_kind                train to a station at a known moment ('actual')
        cancelled                true for "Cancellation Reported"
        less_accurate            true when the text ends "(Less Accurate)"
        raw_status               the text itself; kept in the raw archive, never published
    """
    match = _STATUS_RE.match(status.strip())
    if match is None:
        return Observation(
            observed_at=fetched_at,
            train_number=train_number,
            station_code="",
            event="unknown",
            source=SOURCE,
            raw_status=status,
        )

    observed_at = fetched_at - timedelta(minutes=int(match["ago"] or 0))
    delay = None
    if match["delay"] is not None:
        delay = float(match["delay"]) * (-1 if match["direction"] == "Early" else 1)

    if match["cancelled"]:
        event, station_name = "cancelled", None
    elif match["reaching"]:
        # "Reaching CSMT (at SION now)": the train is at SION, heading for its terminus.
        event, station_name = "at", match["reaching_now"]
    else:
        event = next(
            key for key in ("rake_at", "at", "crossed", "arriving", "between") if match[key]
        )
        # For "between", station_code is the station the train last passed.
        station_name = match[event]

    # Only "at" pins the train to a station at a known instant, so only it gets a time.
    at_station = event == "at"
    return Observation(
        observed_at=observed_at,
        train_number=train_number,
        station_code=resolve_station(station_name, train_number) if station_name else "",
        event=event,
        source=SOURCE,
        delay_minutes=delay,
        actual_or_expected_time=observed_at if at_station else None,
        time_kind="actual" if at_station else None,
        cancelled=event == "cancelled",
        less_accurate=match["less_accurate"] is not None,
        raw_status=status,
    )
