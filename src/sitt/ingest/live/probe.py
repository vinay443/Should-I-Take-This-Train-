"""Fetch each source once and report what came back. Stores nothing.

    python -m sitt.ingest.live.probe [mobond] [ntes]

Used by .github/workflows/probe.yml to check whether the sources answer requests from
GitHub's (non-Indian) runner IPs.

Mobond is only probed when both of its switches are set (see sitt.ingest.live.mobond).
Otherwise the probe says so and makes no request to it.
"""

import re
import sys
from collections.abc import Sequence

from sitt.ingest.live import mobond
from sitt.ingest.live.collect import SOURCES
from sitt.ingest.live.common import SourceError, make_client, user_agent


def _sample(body: str, limit: int = 400) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", body, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())[:limit]


def main(argv: Sequence[str] | None = None) -> int:
    names = list(argv if argv is not None else sys.argv[1:]) or list(SOURCES)
    unknown = [name for name in names if name not in SOURCES]
    if unknown:
        print(f"Unknown source(s): {', '.join(unknown)}. Choose from: {', '.join(SOURCES)}")
        return 2

    print(f"User-Agent: {user_agent()}")
    failures = 0
    with make_client() as client:
        for name in names:
            fetch, parse = SOURCES[name]
            print(f"\n=== {name}")
            try:
                raw = fetch(client)
            except mobond.MobondSkipped as exc:
                # Not a failure: no request was made, on purpose.
                print(f"  NOT FETCHED: {exc}")
                continue
            except SourceError as exc:
                failures += 1
                print("\n".join(f"  {line}" for line in exc.requests))
                print(f"  FAILED: {exc}")
                continue
            print("\n".join(f"  {line}" for line in raw.requests))
            print(f"  content-type: {raw.content_type or '?'}, {len(raw.body)} characters")
            print(f"  sample: {_sample(raw.body)}")
            try:
                observations = parse(raw)
            except SourceError as exc:
                failures += 1
                print(f"  PARSE FAILED: {exc}")
                continue
            print(f"  parsed {len(observations)} observations, e.g.")
            for o in observations[:3]:
                print(f"    {o.train_number} {o.event} {o.station_code} delay={o.delay_minutes}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
