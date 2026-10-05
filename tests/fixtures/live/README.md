# Live-source fixtures

## `mobond_getalllivetrains.json`: hand-written, and may be inaccurate

**This is not a capture of Mobond's feed.** No request has been made to Mobond or
m-Indicator to produce it, and no copy of their data is in this repository.

It was written by hand from the adapter's own parsing code
(`src/sitt/ingest/live/mobond.py`): a JSON object of `train number -> status text`, with one
or more entries for every wording the parser handles (`At X`, `Between X - Y`, `Crossed X`,
`Arriving X`, `Reaching X (at Y now)`, `Rake at X`, `Cancellation Reported`, the
`N min Late/Early` suffix, the `[N min ago]` prefix and the `(Less Accurate)` suffix), plus
one line the parser should not understand. The train numbers, stations and delays are
invented.

What that means for the tests that use it:

- They show that the adapter parses the shapes it was written for, and maps them onto the
  `observations` columns as documented.
- They **do not** show that those are the shapes Mobond sends. The wording was noted during
  a manual look at the feed in September 2026 (`docs/data-sources.md`, section 2.1), and the
  feed may have changed since, or may use wordings that weren't seen then.

When Mobond's permission arrives and the adapter is switched on, expect to revisit this: any
status text the parser doesn't recognise is stored with `event = 'unknown'`, and `sitt-dq`
counts those. See "Turning Mobond on" in `docs/data-sources.md`.

## `ntes_live_station_kyn.html`: real, trimmed

A trimmed copy of the NTES Live Station board for Kalyan, saved on 2026-09-27.
