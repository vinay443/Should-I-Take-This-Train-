"""Types and HTTP helpers shared by the live sources."""

import os
from dataclasses import dataclass
from datetime import datetime

import httpx

# One attempt per request, no retries: a failed source simply waits for the next run.
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

DEFAULT_USER_AGENT = (
    "should-i-take-this-train/0.1 (personal research project; "
    "one request per source at most every 15 minutes)"
)


def user_agent() -> str:
    """An honest User-Agent. `SITT_USER_AGENT` overrides it; on GitHub Actions it links the repo."""
    if override := os.environ.get("SITT_USER_AGENT"):
        return override
    if repo := os.environ.get("GITHUB_REPOSITORY"):
        return f"{DEFAULT_USER_AGENT[:-1]}; +https://github.com/{repo})"
    return DEFAULT_USER_AGENT


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": user_agent()},
        timeout=TIMEOUT,
        follow_redirects=True,
        transport=transport,
    )


class SourceError(Exception):
    """A source could not be fetched or its response could not be understood."""

    def __init__(self, message: str, requests: tuple[str, ...] = ()):
        super().__init__(message)
        self.requests = requests


@dataclass(frozen=True)
class RawResponse:
    """What a source returned, kept verbatim so parsing can be redone later."""

    source: str
    url: str
    fetched_at: datetime  # timezone-aware
    status_code: int
    content_type: str
    body: str
    requests: tuple[str, ...]  # one line per HTTP request made, for logs and the probe


@dataclass(frozen=True)
class Observation:
    """One reading, shaped like a row of `observations` (see schema.sql)."""

    observed_at: datetime  # timezone-aware; when the source says the reading applies
    train_number: str  # exactly as the source gave it
    station_code: str  # IR code where known, else the source's station name; '' if none
    event: str  # arrival/departure (NTES); at/arriving/crossed/between/rake_at/cancelled (Mobond)
    source: str
    delay_minutes: float | None = None  # positive = late, negative = early, None = not given
    actual_or_expected_time: datetime | None = None
    time_kind: str | None = None  # 'actual' | 'expected' | None
    cancelled: bool = False
    less_accurate: bool = False
    raw_status: str | None = None


def request(
    client: httpx.Client, method: str, url: str, log: list[str], **kwargs
) -> httpx.Response:
    """Make one request, record it in `log`, and raise SourceError on failure or HTTP >= 400."""
    try:
        response = client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        log.append(f"{method} {url} -> {type(exc).__name__}: {exc}")
        raise SourceError(f"{method} {url} failed: {exc}", tuple(log)) from exc
    log.append(f"{method} {response.url} -> {response.status_code}, {len(response.content)} bytes")
    if response.status_code >= 400:
        raise SourceError(f"{method} {url} returned HTTP {response.status_code}", tuple(log))
    return response
