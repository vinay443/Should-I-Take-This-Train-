"""Run the live sources once: fetch, archive the raw response, parse, write one Parquet batch."""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from sitt.ingest.live import mobond, ntes
from sitt.ingest.live.common import Observation, RawResponse, SourceError
from sitt.ingest.live.storage import new_batch_id, write_parquet, write_raw

logger = logging.getLogger(__name__)

Fetch = Callable[[httpx.Client], RawResponse]
Parse = Callable[[RawResponse], list[Observation]]

SOURCES: dict[str, tuple[Fetch, Parse]] = {
    mobond.SOURCE: (mobond.fetch, mobond.parse_response),
    ntes.SOURCE: (ntes.fetch, ntes.parse_response),
}


@dataclass
class SourceResult:
    source: str
    observations: list[Observation] = field(default_factory=list)
    raw_path: Path | None = None
    error: str | None = None
    # Why the source was deliberately not fetched this run (Mobond's opt-in, rate limit
    # or backoff). Not a failure.
    skipped: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class CollectResult:
    batch_id: str
    sources: list[SourceResult]
    parquet_path: Path | None = None

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.sources)

    @property
    def observations(self) -> list[Observation]:
        return [o for result in self.sources for o in result.observations]


def collect(
    sources: Sequence[str],
    client: httpx.Client,
    *,
    observations_dir: Path,
    raw_dir: Path,
    dry_run: bool = False,
    now: datetime | None = None,
    state_dir: Path | None = None,
) -> CollectResult:
    """Fetch each source once, in order. A failing source never stops the others.

    `state_dir` is where Mobond's rate-limit state lives (default `data/logs`).
    """
    result = CollectResult(batch_id=new_batch_id(now or datetime.now(UTC)), sources=[])
    for source in sources:
        fetch, parse = SOURCES[source]
        source_result = SourceResult(source)
        result.sources.append(source_result)
        options = {}
        if source == mobond.SOURCE and state_dir is not None:
            options["state_file"] = Path(state_dir) / mobond.STATE_FILE
        try:
            raw = fetch(client, **options)
            if not dry_run:
                # Archive before parsing, so a parser bug never loses the response.
                source_result.raw_path = write_raw(raw, raw_dir, result.batch_id)
            source_result.observations = parse(raw)
        except mobond.MobondSkipped as exc:
            source_result.skipped = str(exc)
            logger.info("%s", exc)
        except SourceError as exc:
            source_result.error = str(exc)
            logger.error("%s failed: %s", source, exc)
            for line in exc.requests:
                logger.info("  %s", line)
        except Exception as exc:  # a bug in one source mustn't stop the next
            source_result.error = f"{type(exc).__name__}: {exc}"
            logger.exception("%s failed unexpectedly", source)

    observations = result.observations
    if observations and not dry_run:
        result.parquet_path = write_parquet(observations, observations_dir, result.batch_id)
    return result
