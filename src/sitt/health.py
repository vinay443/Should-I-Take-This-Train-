"""Is the collector healthy? A report, a daily Telegram summary, and alerts.

    uv run sitt-health                  the last 24 hours, printed
    uv run sitt-health --date 2026-10-05
    uv run sitt-health --telegram       send the compact summary to the allowed users
    uv run sitt-health --alert-only     send a message only if something is wrong

Reads `collector_runs` (written by `sitt-collect`) and `observations`. Two kinds of
trouble are kept apart:

* a **gap**: no run was recorded for longer than the cadence allows. The machine was off
  or asleep, or Task Scheduler didn't start the task.
* a **source failure**: a run happened, and a source didn't answer or couldn't be parsed.

Telegram messages are only really sent when `SITT_TELEGRAM_SEND=true`; otherwise, and
with `--dry-run`, they are printed. All times shown are Asia/Kolkata. See docs/collector.md.
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import duckdb
import httpx
from dotenv import find_dotenv, load_dotenv

from sitt import notify
from sitt.config import HealthSettings, load_health_settings, load_settings
from sitt.db import DatabaseBusyError, open_with_retry
from sitt.ingest.live.runlog import FAILED, OK, PARTIAL, SKIPPED_DISABLED
from sitt.tz import IST

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

UP, DOWN, DISABLED, NO_RUNS = "up", "down", "disabled", "no runs"


def _moment(micros: int | None) -> datetime | None:
    return None if micros is None else _EPOCH + timedelta(microseconds=micros)


def _has_table(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return (
        con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]
        ).fetchone()[0]
        > 0
    )


# --- runs and gaps ---


@dataclass(frozen=True)
class Run:
    run_id: str
    source: str
    started_at: datetime
    status: str
    readings: int
    error: str | None


@dataclass(frozen=True)
class Gap:
    """A stretch with no run recorded at all."""

    start: datetime
    end: datetime
    missed_runs: int

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


def load_runs(
    con: duckdb.DuckDBPyConnection, start: datetime | None = None, end: datetime | None = None
) -> list[Run]:
    """Run rows whose start falls in [start, end], oldest first."""
    if not _has_table(con, "collector_runs"):
        return []
    rows = con.execute(
        """
        SELECT run_id, source, epoch_us(started_at), status, readings, error
        FROM collector_runs
        WHERE (?::TIMESTAMPTZ IS NULL OR started_at >= ?::TIMESTAMPTZ)
          AND (?::TIMESTAMPTZ IS NULL OR started_at <= ?::TIMESTAMPTZ)
        ORDER BY started_at, run_id, source
        """,
        [start, start, end, end],
    ).fetchall()
    return [Run(r[0], r[1], _moment(r[2]), r[3], r[4], r[5]) for r in rows]


def find_gaps(
    run_times: Sequence[datetime],
    start: datetime,
    end: datetime,
    cadence_minutes: float,
    tolerance_minutes: float,
) -> list[Gap]:
    """Stretches of [start, end] where runs are further apart than the cadence allows.

    `run_times` are the start times of the runs that happened. A stretch counts as a gap
    when it is longer than the cadence plus the tolerance. The edges of the window count
    too: no run between `start` and the first run, or between the last run and `end`.
    """
    cadence = timedelta(minutes=cadence_minutes)
    limit = cadence + timedelta(minutes=tolerance_minutes)
    times = sorted(t for t in run_times if start <= t <= end)
    gaps = []
    edges = [start, *times, end]
    for index in range(1, len(edges)):
        previous, moment = edges[index - 1], edges[index]
        stretch = moment - previous
        if stretch > limit:
            intervals = stretch / cadence
            # Between two runs, one interval is the normal wait for the next run.
            between_runs = 1 < index < len(edges) - 1
            missed = round(intervals) - 1 if between_runs else int(intervals)
            gaps.append(Gap(previous, moment, max(1, missed)))
    return gaps


# --- the report ---


@dataclass(frozen=True)
class Outage:
    """Consecutive runs in which one source failed."""

    start: datetime
    end: datetime
    runs: int
    error: str | None


@dataclass
class SourceHealth:
    source: str
    state: str = NO_RUNS
    ok: int = 0
    partial: int = 0
    failed: int = 0
    skipped: int = 0
    readings: int = 0
    trains_matched: int = 0
    trains_unmatched: int = 0
    outages: list[Outage] = field(default_factory=list)


@dataclass
class HealthReport:
    start: datetime
    end: datetime
    generated_at: datetime
    cadence_minutes: float
    expected_runs: int
    actual_runs: int
    gaps: list[Gap]
    sources: list[SourceHealth]
    newest_observation_at: datetime | None
    has_run_log: bool
    # One line per data-quality headline, filled by sitt.dq when it is available.
    data_quality: list[str] = field(default_factory=list)

    @property
    def missed_runs(self) -> int:
        return sum(gap.missed_runs for gap in self.gaps)

    @property
    def newest_observation_age(self) -> timedelta | None:
        if self.newest_observation_at is None:
            return None
        return self.generated_at - self.newest_observation_at


def _source_health(source: str, runs: Sequence[Run]) -> SourceHealth:
    health = SourceHealth(source)
    streak: list[Run] = []

    def close_streak() -> None:
        if streak:
            health.outages.append(
                Outage(streak[0].started_at, streak[-1].started_at, len(streak), streak[-1].error)
            )
            streak.clear()

    for run in runs:
        if run.status == SKIPPED_DISABLED:
            health.skipped += 1
            continue
        health.readings += run.readings
        if run.status == FAILED:
            health.failed += 1
            streak.append(run)
            continue
        close_streak()
        if run.status == OK:
            health.ok += 1
        else:
            health.partial += 1
    close_streak()

    attempted = [run for run in runs if run.status != SKIPPED_DISABLED]
    if attempted:
        health.state = DOWN if attempted[-1].status == FAILED else UP
    elif health.skipped:
        health.state = DISABLED
    return health


def _train_counts(
    con: duckdb.DuckDBPyConnection, start: datetime, end: datetime
) -> dict[str, tuple[int, int]]:
    rows = con.execute(
        """
        SELECT source,
               count(DISTINCT train_number) FILTER (WHERE train_id <> train_number),
               count(DISTINCT train_number) FILTER (WHERE train_id = train_number)
        FROM observations
        WHERE observed_at >= ? AND observed_at <= ? AND source <> 'synthetic'
        GROUP BY source
        """,
        [start, end],
    ).fetchall()
    return {source: (matched, unmatched) for source, matched, unmatched in rows}


DataQualityHook = Callable[[duckdb.DuckDBPyConnection, datetime, datetime], list[str]]


def build_report(
    con: duckdb.DuckDBPyConnection,
    start: datetime,
    end: datetime,
    settings: HealthSettings | None = None,
    now: datetime | None = None,
    data_quality: DataQualityHook | None = None,
) -> HealthReport:
    """The collector's health between two instants (timezone-aware)."""
    settings = settings or HealthSettings()
    now = now or datetime.now(UTC)
    has_run_log = _has_table(con, "collector_runs")
    runs = load_runs(con, start, end)

    # Don't count the time before the collector was first switched on as a gap.
    effective_start = start
    if has_run_log:
        first = _moment(
            con.execute("SELECT epoch_us(min(started_at)) FROM collector_runs").fetchone()[0]
        )
        if first is not None and start < first <= end:
            effective_start = first

    run_times = sorted({run.run_id: run.started_at for run in runs}.values())
    by_source: dict[str, list[Run]] = {}
    for run in runs:
        by_source.setdefault(run.source, []).append(run)
    sources = [_source_health(source, by_source[source]) for source in sorted(by_source)]

    has_observations = _has_table(con, "observations")
    if has_observations:
        counts = _train_counts(con, start, end)
        for health in sources:
            health.trains_matched, health.trains_unmatched = counts.get(health.source, (0, 0))
        newest = _moment(
            con.execute(
                "SELECT epoch_us(max(observed_at)) FROM observations "
                "WHERE source <> 'synthetic' AND observed_at <= ?",
                [now],
            ).fetchone()[0]
        )
    else:
        newest = None

    cadence = timedelta(minutes=settings.cadence_minutes)
    return HealthReport(
        start=start,
        end=end,
        generated_at=now,
        cadence_minutes=settings.cadence_minutes,
        expected_runs=max(0, int((end - effective_start) / cadence)),
        actual_runs=len(run_times),
        gaps=find_gaps(
            run_times,
            effective_start,
            end,
            settings.cadence_minutes,
            settings.gap_tolerance_minutes,
        ),
        sources=sources,
        newest_observation_at=newest,
        has_run_log=has_run_log,
        data_quality=data_quality(con, start, end) if data_quality and has_observations else [],
    )


# --- formatting (all times Asia/Kolkata) ---


def _ist(moment: datetime, with_day: bool = True) -> str:
    return f"{moment.astimezone(IST):{'%d %b %H:%M' if with_day else '%H:%M'}}"


def _span(start: datetime, end: datetime) -> str:
    same_day = start.astimezone(IST).date() == end.astimezone(IST).date()
    return f"{_ist(start)} to {_ist(end, with_day=not same_day)}"


def duration(delta: timedelta) -> str:
    minutes = max(0, round(delta.total_seconds() / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {rest} min" if rest else f"{hours} h"
    return f"{hours // 24} d {hours % 24} h"


def _age_line(report: HealthReport) -> str:
    if report.newest_observation_at is None:
        return "Newest observation: none"
    return (
        f"Newest observation: {_ist(report.newest_observation_at)} IST "
        f"({duration(report.newest_observation_age)} ago)"
    )


def _source_line(health: SourceHealth) -> str:
    if health.state == DISABLED:
        return f"  {health.source}: DISABLED (switched off in {health.skipped} run(s))"
    return (
        f"  {health.source}: {health.state.upper()}; runs ok {health.ok}, "
        f"partial {health.partial}, failed {health.failed}; {health.readings:,} readings; "
        f"trains matched {health.trains_matched}, unmatched {health.trains_unmatched}"
    )


def format_report(report: HealthReport) -> str:
    """The full report, for the terminal."""
    lines = [
        f"Collector health, {_span(report.start, report.end)} IST "
        f"({duration(report.end - report.start)})",
    ]
    if not report.has_run_log:
        lines.append(
            "This database has no run log yet. Run `uv run sitt-collect` once to create it."
        )
    lines.append(
        f"Runs: {report.actual_runs} recorded, about {report.expected_runs} expected "
        f"at one every {report.cadence_minutes:g} min"
    )
    if report.gaps:
        lines.append(
            f"Gaps: {len(report.gaps)}, about {report.missed_runs} run(s) missed. No run was "
            "recorded in these stretches (machine off or asleep, or the task didn't start):"
        )
        lines += [
            f"  {_span(gap.start, gap.end)}: {duration(gap.end - gap.start)}, "
            f"about {gap.missed_runs} run(s)"
            for gap in report.gaps
        ]
    else:
        lines.append("Gaps: none")
    lines.append("Sources:" if report.sources else "Sources: no runs recorded in this window")
    for health in report.sources:
        lines.append(_source_line(health))
        lines += [
            f"    failed {_span(outage.start, outage.end)} ({outage.runs} run(s))"
            + (f": {outage.error}" if outage.error else "")
            for outage in health.outages
        ]
    lines.append(_age_line(report))
    if report.data_quality:
        lines.append("Data quality:")
        lines += [f"  {line}" for line in report.data_quality]
    return "\n".join(lines)


def format_summary(report: HealthReport) -> str:
    """A compact version of the report, for one Telegram message."""
    lines = [
        f"SITT collector, {_span(report.start, report.end)} IST",
        f"Runs: {report.actual_runs} of about {report.expected_runs}",
    ]
    if report.gaps:
        longest = max(report.gaps, key=lambda gap: gap.minutes)
        lines.append(
            f"Gaps: {len(report.gaps)} (about {report.missed_runs} runs missed); longest "
            f"{_span(longest.start, longest.end)}"
        )
    else:
        lines.append("Gaps: none")
    for health in report.sources:
        if health.state == DISABLED:
            lines.append(f"{health.source}: disabled")
            continue
        line = (
            f"{health.source}: {health.state.upper()}, {health.readings:,} readings, "
            f"{health.failed} failed run(s), trains matched {health.trains_matched} / "
            f"unmatched {health.trains_unmatched}"
        )
        lines.append(line)
    if not report.sources:
        lines.append("No runs recorded in this window.")
    lines.append(_age_line(report))
    lines += [f"DQ: {line}" for line in report.data_quality]
    return "\n".join(lines)


# --- alerts ---


@dataclass(frozen=True)
class Alert:
    key: str
    message: str


def evaluate_alerts(
    con: duckdb.DuckDBPyConnection, now: datetime, settings: HealthSettings | None = None
) -> list[Alert]:
    """The problems present right now. Looks as far back as each rule needs."""
    settings = settings or HealthSettings()
    runs = load_runs(con, end=now)
    alerts = []

    # 1. No run has succeeded for too long (or ever).
    succeeded = [run.started_at for run in runs if run.status == OK]
    limit = timedelta(hours=settings.alert_no_run_hours)
    if not succeeded:
        alerts.append(Alert("no_successful_run", "No successful collector run is recorded."))
    elif now - max(succeeded) > limit:
        alerts.append(
            Alert(
                "no_successful_run",
                f"No successful collector run for {duration(now - max(succeeded))} "
                f"(last: {_ist(max(succeeded))} IST). Is the machine awake and the "
                "scheduled task running?",
            )
        )

    by_source: dict[str, list[Run]] = {}
    for run in runs:
        if run.status != SKIPPED_DISABLED:
            by_source.setdefault(run.source, []).append(run)
    for source, attempted in sorted(by_source.items()):
        # 2. The source failed in each of its last N runs.
        latest = attempted[-settings.alert_source_down_runs :]
        if len(latest) == settings.alert_source_down_runs and all(
            run.status == FAILED for run in latest
        ):
            streak = 0
            for run in reversed(attempted):
                if run.status != FAILED:
                    break
                streak += 1
            error = f" Last error: {latest[-1].error}" if latest[-1].error else ""
            alerts.append(
                Alert(
                    f"source_down:{source}",
                    f"{source} has failed in its last {streak} runs, since "
                    f"{_ist(attempted[-streak].started_at)} IST.{error}",
                )
            )
            continue
        # 3. The source answers but gives far fewer readings than usual.
        answered = [run for run in attempted if run.status in (OK, PARTIAL)]
        answered = answered[-settings.alert_readings_runs :]
        if len(answered) == settings.alert_readings_runs:
            mean = sum(run.readings for run in answered) / len(answered)
            if mean < settings.alert_min_readings:
                alerts.append(
                    Alert(
                        f"low_readings:{source}",
                        f"{source} averaged {mean:.1f} readings over its last "
                        f"{len(answered)} runs (alert threshold "
                        f"{settings.alert_min_readings:g}). Its page layout may have changed.",
                    )
                )
    return alerts


@dataclass(frozen=True)
class AlertDecision:
    """What to send now, and the `alert_state` rows that sending it implies."""

    messages: list[str]
    state_rows: list[tuple]  # (alert_key, active, first_seen_at, last_sent_at, last_message)


def load_alert_state(con: duckdb.DuckDBPyConnection) -> dict[str, tuple]:
    """alert_key -> (active, first_seen_at, last_sent_at, last_message)."""
    if not _has_table(con, "alert_state"):
        return {}
    rows = con.execute(
        "SELECT alert_key, active, epoch_us(first_seen_at), epoch_us(last_sent_at), last_message "
        "FROM alert_state"
    ).fetchall()
    return {
        key: (active, _moment(first), _moment(sent), message)
        for key, active, first, sent, message in rows
    }


def decide_alerts(
    alerts: Sequence[Alert], state: dict[str, tuple], now: datetime, settings: HealthSettings
) -> AlertDecision:
    """Dedupe: a new problem is sent once, a lasting one is repeated only every
    `alert_repeat_hours`, and a problem that has gone is announced as recovered once."""
    repeat = timedelta(hours=settings.alert_repeat_hours)
    messages, rows = [], []
    current = {alert.key: alert for alert in alerts}
    for key, alert in current.items():
        active, first_seen, last_sent, _ = state.get(key, (False, None, None, None))
        if not active:
            messages.append(f"ALERT: {alert.message}")
            rows.append((key, True, now, now, alert.message))
        elif last_sent is None or now - last_sent >= repeat:
            messages.append(
                f"STILL: {alert.message} (first reported {_ist(first_seen or now)} IST)"
            )
            rows.append((key, True, first_seen or now, now, alert.message))
    for key, (active, first_seen, _last_sent, message) in state.items():
        if active and key not in current:
            messages.append(f"RECOVERED: {key.replace('_', ' ').replace(':', ' ')} is fine again.")
            rows.append((key, False, first_seen or now, now, message))
    return AlertDecision(messages, rows)


def save_alert_state(con: duckdb.DuckDBPyConnection, rows: Sequence[tuple]) -> None:
    if rows:
        con.executemany(
            "INSERT OR REPLACE INTO alert_state "
            "(alert_key, active, first_seen_at, last_sent_at, last_message) VALUES (?, ?, ?, ?, ?)",
            [list(row) for row in rows],
        )


# --- CLI ---


def window_for(
    now: datetime, hours: float = 24.0, day: date | None = None
) -> tuple[datetime, datetime]:
    """[start, end] for the report: the last `hours`, or one whole Mumbai calendar day."""
    if day is None:
        return now - timedelta(hours=hours), now
    start = datetime.combine(day, time(0), tzinfo=IST)
    end = start + timedelta(days=1)
    # Today so far, when the day isn't over yet: the hours still to come aren't a gap.
    return start, now if start < now < end else end


def _data_quality_hook() -> DataQualityHook | None:
    try:
        from sitt import dq
    except ImportError:
        return None
    return dq.headline


def deliver(
    text: str,
    *,
    token: str | None,
    user_ids: frozenset[int],
    really_send: bool,
    client: httpx.Client | None = None,
    out=None,
) -> bool:
    """Send `text` to the allowed users, or print it when not really sending.

    Returns whether it was sent. Raises notify.NotifyError if sending was attempted and failed.
    """
    out = out or sys.stdout
    if not really_send:
        print(f"[dry run] would send to {len(user_ids)} Telegram user(s):\n{text}", file=out)
        return False
    if not token or not user_ids:
        raise notify.NotifyError(
            "TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS must both be set to send (see "
            "docs/bot-setup.md)"
        )
    sent = notify.send_to_users(token, user_ids, text, client)
    print(f"Sent to {sent} Telegram user(s).", file=out)
    return True


def main(
    argv: Sequence[str] | None = None,
    *,
    now: datetime | None = None,
    client: httpx.Client | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="sitt-health",
        description="Report on the live collector: runs, gaps, sources and data freshness.",
        epilog="Times are Asia/Kolkata. Telegram messages are only really sent when "
        "SITT_TELEGRAM_SEND=true is set; otherwise they are printed. See docs/collector.md.",
    )
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--hours", type=float, default=24.0, help="look back this far (default 24)")
    window.add_argument(
        "--date", type=date.fromisoformat, help="report on one Mumbai calendar day, YYYY-MM-DD"
    )
    parser.add_argument("--db", type=Path, help="default: SITT_DB_PATH or data/sitt.duckdb")
    parser.add_argument(
        "--telegram", action="store_true", help="send the compact summary to ALLOWED_USER_IDS"
    )
    parser.add_argument(
        "--alert-only",
        action="store_true",
        help="send a Telegram message only if something is wrong (or has just recovered)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would be sent; send and store nothing"
    )
    args = parser.parse_args(argv)

    load_dotenv(find_dotenv(usecwd=True))
    try:
        settings = load_settings()
        health = load_health_settings()
    except ValueError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    db_path = args.db or settings.db_path
    now = now or datetime.now(UTC)
    really_send = health.telegram_send and not args.dry_run

    try:
        with open_with_retry(db_path, read_only=True) as con:
            if args.alert_only:
                decision = decide_alerts(
                    evaluate_alerts(con, now, health), load_alert_state(con), now, health
                )
            else:
                start, end = window_for(now, args.hours, args.date)
                report = build_report(con, start, end, health, now, _data_quality_hook())
    except FileNotFoundError as exc:
        print(f"{exc}. Run `uv run sitt-init-db` or `uv run sitt-collect` first.", file=sys.stderr)
        return 2
    except DatabaseBusyError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    delivery = dict(
        token=settings.telegram_bot_token,
        user_ids=settings.allowed_user_ids,
        really_send=really_send,
        client=client,
    )
    try:
        if not args.alert_only:
            print(format_report(report))
            if args.telegram:
                print()
                deliver(format_summary(report), **delivery)
            return 0

        if not decision.messages:
            print("Nothing to report: no new alerts.")
            return 0
        sent = deliver("SITT collector\n" + "\n".join(decision.messages), **delivery)
    except notify.NotifyError as exc:
        print(f"Could not send: {exc}", file=sys.stderr)
        return 3

    if sent:
        # Remember what was said, so the next check doesn't repeat it.
        try:
            with open_with_retry(db_path) as con:
                save_alert_state(con, decision.state_rows)
        except DatabaseBusyError as exc:
            print(
                f"The alert was sent but could not be recorded, so it may repeat: {exc}",
                file=sys.stderr,
            )
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
