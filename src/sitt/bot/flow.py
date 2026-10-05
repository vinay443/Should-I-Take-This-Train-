"""State for the guided `/log` flow. No Telegram imports here."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Literal

from sitt.bot.parsing import ParsedLog, Service, parse_crowd_level, parse_service, parse_time
from sitt.bot.stations import FALLBACK_DIRECTORY, StationDirectory

Step = Literal["station", "time", "service", "crowd"]
STEPS: tuple[Step, ...] = ("station", "time", "service", "crowd")

CALLBACK_PREFIX = "log"
CANCEL_DATA = f"{CALLBACK_PREFIX}:cancel"

# Offsets (minutes) from now for the departure-time buttons. Reports usually
# come in after boarding, so the window leans into the past.
TIME_CHOICE_OFFSETS: tuple[int, ...] = (-50, -40, -30, -20, -10, 0, 10, 20)


@dataclass
class LogDraft:
    """A crowd report being filled in, one missing field at a time."""

    raw_text: str
    station_code: str | None = None
    departure_time: time | None = None
    service: Service | None = None
    crowd_level: int | None = None
    message_id: int | None = None  # message carrying the current keyboard
    # Optional details from a quick log, used only to find the exact train.
    destination_code: str | None = None
    is_ac: bool | None = None
    car_count: int | None = None
    ladies: bool | None = None

    @classmethod
    def from_parsed(cls, parsed: ParsedLog, raw_text: str) -> "LogDraft":
        return cls(
            raw_text=raw_text,
            station_code=parsed.station_code,
            departure_time=parsed.departure_time,
            service=parsed.service,
            crowd_level=parsed.crowd_level,
            destination_code=parsed.destination_code,
            is_ac=parsed.is_ac,
            car_count=parsed.car_count,
            ladies=parsed.ladies,
        )

    def next_step(self) -> Step | None:
        values = {
            "station": self.station_code,
            "time": self.departure_time,
            "service": self.service,
            "crowd": self.crowd_level,
        }
        return next((step for step in STEPS if values[step] is None), None)

    @property
    def is_complete(self) -> bool:
        return self.next_step() is None

    def train_description(self) -> str:
        """`crowd_reports.train_description` for a complete draft, e.g. "08:12 fast from KYN"."""
        return f"{self.departure_time:%H:%M} {self.service} from {self.station_code}"

    def apply(
        self, step: Step, value: str, stations: StationDirectory = FALLBACK_DIRECTORY
    ) -> None:
        """Set one field from a button or typed value. Raises ValueError if invalid."""
        if step == "station":
            station = stations.lookup(value)
            if station is None:
                raise ValueError(f"unknown station {value!r}")
            self.station_code = station.code
        elif step == "time":
            parsed_time = parse_time(value)
            if parsed_time is None:
                raise ValueError(f"not a time: {value!r}")
            self.departure_time = parsed_time
        elif step == "service":
            service = parse_service(value)
            if service is None:
                raise ValueError(f"not fast/slow: {value!r}")
            self.service = service
        elif step == "crowd":
            level = parse_crowd_level(value)
            if level is None:
                raise ValueError(f"not a crowd level: {value!r}")
            self.crowd_level = level
        else:
            raise ValueError(f"unknown step {step!r}")


def callback_data(step: Step, value: str) -> str:
    return f"{CALLBACK_PREFIX}:{step}:{value}"


def parse_callback_data(data: str) -> tuple[Step, str]:
    """Split `log:<step>:<value>`. Raises ValueError for anything else."""
    prefix, _, rest = data.partition(":")
    step, _, value = rest.partition(":")
    if prefix != CALLBACK_PREFIX or step not in STEPS or not value:
        raise ValueError(f"bad callback data {data!r}")
    return step, value  # type: ignore[return-value]


def time_choices(now: datetime, offsets: tuple[int, ...] = TIME_CHOICE_OFFSETS) -> list[time]:
    """Departure-time buttons: `now` floored to 5 minutes, shifted by `offsets`."""
    base = now.replace(second=0, microsecond=0)
    base -= timedelta(minutes=base.minute % 5)
    return [(base + timedelta(minutes=offset)).time() for offset in offsets]
