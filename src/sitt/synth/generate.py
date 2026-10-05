"""Generate SYNTHETIC live observations for a real timetable.

Nothing here comes from real trains. The generator invents delays from the guesses
written down in docs/synthetic-data.md and `SynthConfig`, then reports them the way the
live collector would have seen them: one reading per running train at each 15-minute
poll, in the collector's own `Observation` shape, with `source = 'synthetic'`.

The steps, each usable on its own:

    simulate(routes, station_seqs, start, days, config)  ->  SynthData (batches, blocks)
    write_batches(batches, observations_dir)             ->  collector-layout Parquet
    build_database(...)                                  ->  a separate DuckDB file

`build_database` loads the Parquet files with the collector's own loader, so the
synthetic database is built exactly as a real one is, and real data needs no new code.
"""

import json
import math
import random
import shutil
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import duckdb

from sitt.config import DEFAULT_DB_PATH
from sitt.db import init_db
from sitt.holidays import is_sunday_schedule
from sitt.ingest.live.common import Observation
from sitt.ingest.live.load import load
from sitt.ingest.live.storage import COLUMNS, _day_dir, to_row
from sitt.routes import Route, load_routes
from sitt.tz import IST

SOURCE = "synthetic"
BATCH_SUFFIX = "synth0"
MARKER_FILE = "SYNTHETIC-DATA.txt"
MARKER_TEXT = (
    "SYNTHETIC DATA. Everything under this folder was invented by `python -m sitt.synth`.\n"
    "It describes no real train. See docs/synthetic-data.md.\n"
)
DAY = 1440


@dataclass(frozen=True)
class BlockTemplate:
    """A stretch of line a megablock closes. Stations are given CSMT end first."""

    from_station: str
    to_station: str
    tracks: str  # 'fast' or 'slow'
    start: str  # HH:MM
    end: str


@dataclass(frozen=True)
class SynthConfig:
    """Every number the generator uses. All of them are guesses; see docs/synthetic-data.md."""

    poll_minutes: int = 15
    missed_poll_share: float = 0.03  # scheduled collector runs that never happen

    # Day to day.
    day_spread: float = 0.35  # sigma of the log-normal "how bad is today" factor
    monsoon_months: tuple[int, ...] = (6, 7, 8, 9)
    monsoon_factor: float = 1.5
    heavy_rain_share: float = 0.15  # of monsoon days
    heavy_rain_factor: float = 2.5

    # Along a route. Delay is in minutes; steps are per station passed.
    origin_delay_mean: float = 0.8
    step_gain: float = 0.04
    step_gain_peak: float = 0.10  # extra per station at the height of the peak
    step_noise: float = 0.25
    recovery_share: float = 0.03  # of the delay above `recovery_above`, won back per station
    recovery_above: float = 3.0
    earliest: float = -2.0  # trains don't run more than this early

    # Peaks, by direction. (start, end) in hours; the hour either side is a half-strength
    # shoulder.
    up_peak: tuple[float, float] = (8.0, 11.0)  # towards CSMT
    down_peak: tuple[float, float] = (17.5, 21.0)  # away from CSMT
    shoulder_strength: float = 0.4

    # The state of the line: a slowly varying delay shared by trains running the same
    # way on the same tracks, which is what makes "the train ahead is late" informative.
    congestion_memory: float = 0.8
    congestion_noise: float = 0.6
    congestion_pull: float = 0.25  # how fast a train's delay rises to the line's
    incident_chance: float = 0.25  # per day
    incident_minutes: tuple[int, int] = (45, 120)
    incident_delay: tuple[float, float] = (8.0, 25.0)

    # Fast trains share tracks with long-distance trains.
    long_distance_chance: float = 0.10
    long_distance_chance_busy: float = 0.22  # up 04-08, down 17-23
    long_distance_delay: tuple[float, float] = (3.0, 10.0)
    long_distance_section: tuple[str, str] = ("TNA", "KYN")

    # Cancellations.
    cancel_chance: float = 0.004
    cancel_rain_factor: float = 3.0
    cancel_chance_in_block: float = 0.12

    # Megablocks: most Sundays, one of these.
    megablock_chance: float = 0.75
    megablock_step_delay: tuple[float, float] = (0.8, 1.8)  # per station in the section
    megablocks: tuple[BlockTemplate, ...] = (
        BlockTemplate("MTN", "MLND", "fast", "11:05", "15:55"),
        BlockTemplate("TNA", "KYN", "slow", "10:40", "15:40"),
        BlockTemplate("CSMT", "VVH", "slow", "10:55", "15:55"),
        BlockTemplate("TNA", "KYN", "fast", "11:00", "16:00"),
    )

    # Invented long-distance trains listed at Kalyan, the way NTES lists real ones. Off by
    # default (0 a day), so the standard synthetic dataset is unchanged. Switched on only
    # to exercise the long-distance feature: `python -m sitt.synth --long-distance`.
    long_distance_trains: int = 0  # per day
    long_distance_station: str = "KYN"
    long_distance_lead_minutes: int = 120  # listed this long before they are due
    long_distance_delay_mean: float = 8.0
    long_distance_congestion_share: float = 1.5  # how strongly they follow the line's state

    # How readings look.
    less_accurate_share: float = 0.4
    less_accurate_noise: int = 2  # +/- minutes


@dataclass
class SynthData:
    batches: dict[str, list[Observation]] = field(default_factory=dict)  # batch_id -> readings
    blocks: list[dict] = field(default_factory=list)  # rows for the `blocks` table
    cancelled_runs: int = 0
    runs: int = 0

    @property
    def observations(self) -> int:
        return sum(len(batch) for batch in self.batches.values())


@dataclass
class _Block:
    day: int  # index of the day it falls on
    start: float  # minutes after midnight
    end: float
    seq_low: int
    seq_high: int
    tracks: str


@dataclass
class _DayState:
    factor: float
    heavy_rain: bool
    congestion: dict[tuple[str, str], list[float]]  # (direction, train_type) -> per 15 min
    block: _Block | None


def _hhmm(text: str) -> float:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def _peak_strength(config: SynthConfig, direction: str, minute_of_day: float) -> float:
    start, end = config.up_peak if direction == "up" else config.down_peak
    hour = minute_of_day / 60
    if start <= hour <= end:
        return 1.0
    if start - 1 <= hour <= end + 1:
        return config.shoulder_strength
    return 0.0


def _day_state(
    config: SynthConfig, rng: random.Random, day_index: int, day: date, seqs: dict[str, int]
) -> tuple[_DayState, dict | None]:
    factor = math.exp(rng.gauss(0, config.day_spread))
    heavy_rain = False
    if day.month in config.monsoon_months:
        factor *= config.monsoon_factor
        if rng.random() < config.heavy_rain_share:
            heavy_rain = True
            factor *= config.heavy_rain_factor

    slots = DAY // 15
    congestion = {}
    for direction in ("up", "down"):
        incident = None
        chance = config.incident_chance * (2 if heavy_rain else 1)
        if rng.random() < chance:
            length = rng.randint(*config.incident_minutes)
            begin = rng.uniform(5 * 60, 22 * 60)
            incident = (begin, begin + length, rng.uniform(*config.incident_delay))
        for train_type in ("fast", "slow"):
            level = 0.0
            series = []
            for slot in range(slots):
                level = config.congestion_memory * level + rng.gauss(0, config.congestion_noise)
                minute = slot * 15 + 7.5
                value = max(0.0, level) * factor * (0.5 + _peak_strength(config, direction, minute))
                if incident and incident[0] <= minute <= incident[1]:
                    # Builds to its worst in the middle of the incident, then clears.
                    middle = (incident[0] + incident[1]) / 2
                    half = (incident[1] - incident[0]) / 2
                    value += incident[2] * (1 - abs(minute - middle) / half)
                series.append(value)
            congestion[(direction, train_type)] = series

    block = None
    block_row = None
    templates = [t for t in config.megablocks if t.from_station in seqs and t.to_station in seqs]
    if day.weekday() == 6 and templates and rng.random() < config.megablock_chance:
        template = rng.choice(templates)
        low, high = sorted((seqs[template.from_station], seqs[template.to_station]))
        block = _Block(
            day_index, _hhmm(template.start), _hhmm(template.end), low, high, template.tracks
        )
        block_row = {
            "block_id": f"synthetic-{day.isoformat()}-{template.from_station}-"
            f"{template.to_station}-{template.tracks}",
            "block_date": day,
            "line": "central",
            "from_station": template.from_station,
            "to_station": template.to_station,
            "start_time": time.fromisoformat(template.start),
            "end_time": time.fromisoformat(template.end),
            "tracks": template.tracks,
            "direction": "both",
            "source": SOURCE,
            "summary": "SYNTHETIC megablock invented by sitt.synth",
        }
    return _DayState(factor, heavy_rain, congestion, block), block_row


def _in_block(block: _Block | None, train_type: str, seq: int, minute: float) -> bool:
    return (
        block is not None
        and block.tracks == train_type
        and block.seq_low <= seq <= block.seq_high
        and block.day * DAY + block.start <= minute <= block.day * DAY + block.end
    )


def batch_id_for(moment: datetime) -> str:
    """Same form as the collector's batch IDs, with a suffix that marks it synthetic."""
    return f"{moment.astimezone(UTC):%Y%m%dT%H%M%SZ}-{BATCH_SUFFIX}"


def simulate(
    routes: Sequence[Route],
    station_seqs: dict[str, int],
    start: date,
    days: int,
    config: SynthConfig | None = None,
    seed: int = 1,
) -> SynthData:
    """Invent `days` days of readings from `start`, one batch per collector poll.

    The same arguments always give the same data.
    """
    config = config or SynthConfig()
    rng = random.Random(seed)
    epoch = datetime.combine(start, time(0), tzinfo=IST)
    data = SynthData()

    states: list[_DayState] = []
    for index in range(days + 1):  # one extra day for trains that run past midnight
        state, block_row = _day_state(
            config, rng, index, start + timedelta(days=index), station_seqs
        )
        states.append(state)
        if block_row and index < days:
            data.blocks.append(block_row)

    polls = range(0, days * DAY, config.poll_minutes)
    missed = {minute for minute in polls if rng.random() < config.missed_poll_share}
    by_poll: dict[int, list[Observation]] = defaultdict(list)
    ld_low, ld_high = sorted(station_seqs.get(code, -1) for code in config.long_distance_section)

    for index in range(days):
        day = start + timedelta(days=index)
        weekday = 6 if is_sunday_schedule(day) else day.weekday()
        for route in routes:
            if route.days[weekday] != "Y":
                continue
            data.runs += 1
            begin = index * DAY + route.run_start.hour * 60 + route.run_start.minute
            points = route.points
            state = states[index]

            through_block = any(
                _in_block(state.block, route.train_type, p.station_seq, begin + p.minutes)
                for p in points
            )
            cancel_chance = config.cancel_chance * (
                config.cancel_rain_factor if state.heavy_rain else 1
            )
            if through_block:
                cancel_chance = config.cancel_chance_in_block
            if rng.random() < cancel_chance:
                data.cancelled_runs += 1
                first = -(-begin // config.poll_minutes) * config.poll_minutes
                for minute in range(first, int(begin + route.duration) + 1, config.poll_minutes):
                    if minute in missed or minute >= days * DAY:
                        continue
                    by_poll[minute].append(
                        Observation(
                            observed_at=epoch + timedelta(minutes=minute),
                            train_number=route.number,
                            station_code="",
                            event="cancelled",
                            source=SOURCE,
                            cancelled=True,
                        )
                    )
                continue

            # Long-distance traffic holds up at most one stretch of a fast train's run.
            held_at = None
            if route.train_type == "fast":
                hour = (begin % DAY) / 60
                busy = (route.direction == "up" and 4 <= hour < 8) or (
                    route.direction == "down" and 17 <= hour < 23
                )
                chance = config.long_distance_chance_busy if busy else config.long_distance_chance
                shared = [
                    i for i, p in enumerate(points) if ld_low <= p.station_seq <= ld_high and i
                ]
                if shared and rng.random() < chance:
                    held_at = rng.choice(shared)

            delay = max(config.earliest, rng.expovariate(1 / config.origin_delay_mean) - 0.3)
            delay *= state.factor if delay > 0 else 1
            delays = [delay]
            for i in range(1, len(points)):
                point = points[i]
                minute = begin + point.minutes
                now = states[min(int(minute // DAY), days)]
                of_day = minute % DAY
                peak = _peak_strength(config, route.direction, of_day)
                gain = rng.gauss(config.step_gain + config.step_gain_peak * peak, config.step_noise)
                delay += gain * now.factor
                if delay > config.recovery_above:
                    delay -= config.recovery_share * (delay - config.recovery_above)
                line = now.congestion[(route.direction, route.train_type)][int(of_day // 15)]
                if line > delay:
                    delay += config.congestion_pull * (line - delay)
                if i == held_at:
                    delay += rng.uniform(*config.long_distance_delay)
                if _in_block(now.block, route.train_type, point.station_seq, minute):
                    delay += rng.uniform(*config.megablock_step_delay)
                delay = max(config.earliest, delay)
                delays.append(delay)

            actual = [begin + p.minutes + d for p, d in zip(points, delays, strict=True)]
            first = int(-(-actual[0] // config.poll_minutes) * config.poll_minutes)
            i = 0
            for minute in range(first, int(actual[-1]) + 1, config.poll_minutes):
                if minute in missed or minute >= days * DAY:
                    continue
                while i + 1 < len(actual) and actual[i + 1] <= minute:
                    i += 1
                # What the source would say about the train right now.
                since = minute - actual[i]
                until = actual[i + 1] - minute if i + 1 < len(actual) else math.inf
                at = i
                if since <= 1.0 and points[i].is_stop:
                    event = "at"
                elif until <= 1.0:
                    event, at = "arriving", i + 1
                elif since <= 2.0:
                    event = "crossed"
                else:
                    event = "between"
                reported = round(delays[at])
                less_accurate = rng.random() < config.less_accurate_share
                if less_accurate:
                    reported += rng.randint(-config.less_accurate_noise, config.less_accurate_noise)
                moment = epoch + timedelta(minutes=minute)
                by_poll[minute].append(
                    Observation(
                        observed_at=moment,
                        train_number=route.number,
                        station_code=points[at].station_code,
                        event=event,
                        source=SOURCE,
                        delay_minutes=float(reported),
                        actual_or_expected_time=moment if event == "at" else None,
                        time_kind="actual" if event == "at" else None,
                        less_accurate=less_accurate,
                    )
                )

    _long_distance_readings(config, seed, states, station_seqs, epoch, days, missed, by_poll)

    for minute in sorted(by_poll):
        data.batches[batch_id_for(epoch + timedelta(minutes=minute))] = by_poll[minute]
    return data


def _long_distance_readings(
    config: SynthConfig,
    seed: int,
    states: Sequence[_DayState],
    station_seqs: dict[str, int],
    epoch: datetime,
    days: int,
    missed: set[int],
    by_poll: dict[int, list[Observation]],
) -> None:
    """Add invented long-distance trains at one station, in the shape of an NTES board.

    Their numbers (11001, 11002, ...) are not in the suburban timetable, which is what
    marks a reading as long-distance. Each runs late by its own amount for the day plus a
    share of the line's congestion at that moment, so the feature built from them carries
    some of the same signal the locals' delays do. A separate random stream is used, so
    switching this on changes nothing else in the data.
    """
    count = config.long_distance_trains
    if count <= 0 or config.long_distance_station not in station_seqs:
        return
    rng = random.Random(f"{seed}-long-distance")
    for index in range(days):
        for k in range(count):
            due = index * DAY + int((k + 0.5) * DAY / count)
            direction = "up" if k % 2 == 0 else "down"
            own = rng.expovariate(1 / config.long_distance_delay_mean) * states[index].factor
            first = -(-(due - config.long_distance_lead_minutes) // config.poll_minutes)
            minute = max(0, first * config.poll_minutes)
            while minute < days * DAY:
                state = states[min(minute // DAY, days)]
                line = state.congestion[(direction, "fast")][(minute % DAY) // 15]
                delay = max(0, round(own + config.long_distance_congestion_share * line))
                if minute > due + delay:
                    break
                if minute not in missed:
                    moment = epoch + timedelta(minutes=minute)
                    by_poll[minute].append(
                        Observation(
                            observed_at=moment,
                            train_number=f"{11001 + k}",
                            station_code=config.long_distance_station,
                            event="arrival",
                            source=SOURCE,
                            delay_minutes=float(delay),
                            actual_or_expected_time=epoch + timedelta(minutes=due + delay),
                            time_kind="expected",
                        )
                    )
                minute += config.poll_minutes


def write_batches(batches: dict[str, list[Observation]], observations_dir: Path) -> int:
    """Write one Parquet file per batch in the collector's layout. Returns the file count.

    Same columns, types and paths as `sitt.ingest.live.storage.write_parquet`, written
    a day at a time because that function is too slow for thousands of batches.
    """
    observations_dir.mkdir(parents=True, exist_ok=True)
    (observations_dir.parent / MARKER_FILE).write_text(MARKER_TEXT, encoding="utf-8")
    by_day: dict[Path, list[str]] = defaultdict(list)
    for batch_id in batches:
        by_day[_day_dir(observations_dir, batch_id)].append(batch_id)

    columns = ", ".join(COLUMNS)
    with duckdb.connect() as con:
        con.execute("SET TimeZone = 'UTC'")
        for folder, batch_ids in by_day.items():
            folder.mkdir(parents=True, exist_ok=True)
            rows = []
            for batch_id in batch_ids:
                for observation in batches[batch_id]:
                    row = to_row(observation, batch_id)
                    for key in ("observed_at", "actual_or_expected_time"):
                        if row[key] is not None:
                            row[key] = row[key].astimezone(UTC).isoformat()
                    rows.append({name: row[name] for name in COLUMNS})
            con.execute(
                f"""
                CREATE OR REPLACE TEMP TABLE day AS
                SELECT unnest(from_json($rows, '{json.dumps([COLUMNS])}'), recursive := true)
                """,
                {"rows": json.dumps(rows)},
            )
            for batch_id in batch_ids:
                target = (folder / f"{batch_id}.parquet").as_posix().replace("'", "''")
                con.execute(
                    f"COPY (SELECT {columns} FROM day WHERE batch_id = ?) "
                    f"TO '{target}' (FORMAT parquet, COMPRESSION zstd)",
                    [batch_id],
                )
    return len(batches)


class SynthError(RuntimeError):
    pass


@dataclass(frozen=True)
class BuildResult:
    db_path: Path
    observations_dir: Path
    start: date
    days: int
    runs: int
    cancelled_runs: int
    batches: int
    observations: int
    blocks: int
    seed: int


def _check_target(db_path: Path, timetable_db: Path) -> None:
    """Refuse to write synthetic data anywhere it could be mistaken for real data."""
    target = db_path.resolve()
    if target in (timetable_db.resolve(), Path(DEFAULT_DB_PATH).resolve()):
        raise SynthError(
            f"refusing to write synthetic data into {db_path}: that is the real database"
        )
    if not db_path.exists():
        return
    with duckdb.connect(str(db_path), read_only=True) as con:
        tables = {name for (name,) in con.execute("SHOW TABLES").fetchall()}
        real = 0
        if "observations" in tables:
            (real,) = con.execute(
                "SELECT count(*) FROM observations WHERE source <> ?", [SOURCE]
            ).fetchone()
        reports = 0
        if "crowd_reports" in tables:
            (reports,) = con.execute("SELECT count(*) FROM crowd_reports").fetchone()
    if real or reports:
        raise SynthError(
            f"refusing to overwrite {db_path}: it holds {real} non-synthetic observations "
            f"and {reports} crowd reports"
        )


def build_database(
    timetable_db: Path,
    db_path: Path,
    out_dir: Path,
    start: date,
    weeks: int,
    seed: int = 1,
    config: SynthConfig | None = None,
) -> BuildResult:
    """Build a fresh synthetic database: the real timetable plus invented observations."""
    _check_target(db_path, timetable_db)
    if not timetable_db.exists():
        raise SynthError(f"no timetable database at {timetable_db}; load a timetable first")
    with duckdb.connect(str(timetable_db), read_only=True) as source:
        routes = load_routes(source)
        seqs = dict(source.execute("SELECT code, seq FROM stations").fetchall())
    if not routes:
        raise SynthError(f"{timetable_db} has no timetable; load one first")

    data = simulate(routes, seqs, start, weeks * 7, config, seed)

    observations_dir = out_dir / "observations"
    if observations_dir.exists():
        if not (out_dir / MARKER_FILE).exists():
            raise SynthError(f"refusing to clear {observations_dir}: no {MARKER_FILE} beside it")
        shutil.rmtree(observations_dir)
    write_batches(data.batches, observations_dir)

    for stale in (db_path, db_path.with_name(db_path.name + ".wal")):
        stale.unlink(missing_ok=True)
    with init_db(db_path) as con:
        source_path = timetable_db.resolve().as_posix().replace("'", "''")
        con.execute(f"ATTACH '{source_path}' AS timetable (READ_ONLY)")
        for table in ("stations", "trains", "scheduled_stops"):
            con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM timetable.{table}")
        con.execute("DETACH timetable")
        load(con, observations_dir)
        if data.blocks:
            now = datetime.now(UTC)
            con.executemany(
                "INSERT INTO blocks (block_id, block_date, line, from_station, to_station, "
                "start_time, end_time, tracks, direction, source, summary, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [[*block.values(), now] for block in data.blocks],
            )
    return BuildResult(
        db_path=db_path,
        observations_dir=observations_dir,
        start=start,
        days=weeks * 7,
        runs=data.runs,
        cancelled_runs=data.cancelled_runs,
        batches=len(data.batches),
        observations=data.observations,
        blocks=len(data.blocks),
        seed=seed,
    )


def config_as_dict(config: SynthConfig | None = None) -> dict:
    return asdict(config or SynthConfig())
