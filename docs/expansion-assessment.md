# Expanding beyond the Central main line: an assessment

Research only, done on **2026-10-05**. Nothing here is implemented. The question: how much
work would it be to add the **Harbour line** (Central Railway) and the **Western line**
(Western Railway), and is it worth doing now?

**Short answer: the timetables are there and free. Harbour is a small job. Western is a
medium one. Neither is worth doing yet, because there is no live delay data for either line
without Mobond's permission, and the timetable alone adds little.**

## 1. Are official timetables available?

Yes, for both, as free PDFs on the railways' own sites.

### Harbour line (Central Railway)

Published on the same page as the main-line timetable:
`https://cr.indianrailways.gov.in/view_section.jsp?lang=0&id=0,5,2360`

| File | In force from |
| --- | --- |
| Harbour DOWN (`DN HB REVISED PTT WEF 01.05.2026.pdf`) | 1 May 2026 |
| Harbour UP (`UP HB REVISED PTT WEF 01.05.2026.pdf`) | 1 May 2026 |
| AC services on the Harbour line (`HB AC PTT WEF 01.05.2026`) | 1 May 2026 |
| Trans-Harbour, Thane–Vashi/Panvel (`THB PTT wef 13.01.2024.pdf`) | 13 January 2024 |
| Port line, Nerul/Belapur–Uran (`PORT LINE PTT WEF 15.12.2025.pdf`) | 15 December 2025 |

The Harbour timetable is **five months old**, which is newer than the main-line base edition
this project already uses (October 2024). The list of links comes from a copy of the page
saved on 2026-09-27; it was not re-read for this assessment.

### Western line (Western Railway)

Published at `https://wr.indianrailways.gov.in/view_section.jsp?lang=0&id=0,6,458`:

| File | In force from |
| --- | --- |
| Pocket Time Table 79, DOWN (`DN TRAINS PTT 79 W.E.F. 01.09.2026.pdf`) | 1 September 2026 |
| Pocket Time Table 79, UP | 1 September 2026 |
| AC EMU services, DOWN and UP (two files) | 1 September 2026 |
| Dahanu Road EMU services | 1 September 2026 |
| Harbour services on Western Railway tracks | 1 October 2022 |

The Western timetable is **five weeks old**. Western Railway numbers its editions (this is
the 79th) and replaces the whole set at once, which is tidier than Central Railway's base
edition plus supplements.

### Getting the files

Both sites **refused a TLS handshake from Python** (`httpx`) on this machine on 2026-10-05
(`SSLV3_ALERT_HANDSHAKE_FAILURE`). One attempt was made at each and no retry. The same files
opened normally through a web fetching tool, which is how one copy of each was obtained for
this assessment (in `scratch/part10/`, not in git).

That matters beyond this assessment: **the dashboard's own download of the main-line PDFs
uses the same `httpx` client, so it will probably fail the same way on this machine.** It
falls back to the sample timetable when that happens, so nothing breaks, but it is worth
knowing. It may be a recent change on the railways' side, or specific to this Python build.
Downloading the PDFs in a browser and passing local paths, as the README describes, works.

## 2. How different are the formats?

The existing converter ([`src/sitt/ingest/cr_pdf.py`](../src/sitt/ingest/cr_pdf.py)) was run
unchanged on the first three pages of each DOWN file.

### Harbour: the same format, new stations

Result: `unknown station 'Mumbai CSMT'` on every page. That is the *only* thing that failed.

| | Main line (works today) | Harbour DOWN |
| --- | --- | --- |
| Pages, size | 18, landscape | 18, landscape, same size |
| Header row | `STATION`/`Stations` + 5-digit train numbers | the same |
| Marker rows | service code, `AC`, `X`/`XX` | the same (`PL 1`, `GN 3`, `AC`, `X`), plus a filler row of `0`s, which the converter already skips for the 15-car supplement |
| Times | `HH:MM` | `HH:MM` |
| Trains | 895 | 351 in the DOWN file, numbered 98xxx and 99xxx (three 91xxx) |
| Passing a station | `…` | not seen: Harbour trains appear to call everywhere they run |

Station rows, as printed: Mumbai CSMT, Masjid, Sandhurst Road, Dockyard Road, Reay Road,
Cotton Green, Sewri, Vadala Road, then the **Goregaon branch** (King's Circle, Mahim Jn,
Bandra, Khar Road, Santacruz, Vileparle, Andheri, Jogeshwari, Ramnagar, Goregaon), then the
**Panvel branch** (GTB Nagar, Chunabhatti, Kurla, Tilaknagar, Chembur, Govandi, Mankhurd,
Vashi, Sanpada, Juinagar, Nerul, Seawoods Darave Karave, Belapur CBD, Kharghar, Mansarovar,
Khandeshwar, Panvel). Branch by branch below a common trunk, exactly as the main line lists
Karjat and Kasara below Kalyan.

What would need doing:

1. **A station table for the Harbour line**: 36 labels to map to codes. Thirty-three are new
   to the converter.
2. **`TNA` written in the CSMT row** of 37 trains. On the main line a station code in a cell
   means "this train really starts there", and the converter accepts it only if the train
   has a time at that station. Thane isn't a row on this page: these are Trans-Harbour
   trains listed on the Harbour page. They need a rule (skip them here and take them from
   the Trans-Harbour PDF).
3. **Shared stations.** CSMT, Masjid, Sandhurst Road and Kurla are on both lines, and the
   database gives a station exactly one line (see the limitation in
   [`timetable-format.md`](timetable-format.md#loading)). Loading the Harbour line today
   would move CSMT to it. Kurla is two different sets of platforms. This needs
   `stations` splitting into stations and station-lines, which touches the loader, the
   timetable queries, the route model and the bot's station lookup.
4. **Fast/slow** would be `slow` for everything, which is right.

### Western: the same idea, a different layout

Result: `no header row with train numbers` on every page.

| | Main line (works today) | Western DOWN |
| --- | --- | --- |
| Pages | 18 | 56, about 13 trains a page |
| Header | one row: `STATION` + train numbers | `STATIONS` + **destination codes** (`VR`, `BVI`, `DRD`), with the train numbers on the **next** row |
| Marker rows | service code, `AC`, `X`/`XX` | a row labelled `DN TRAINS` holding `12 CAR` / `15 CAR`; then free text in the grid: `NOT ON SUN`, `SUN ONLY`, `Air Condition`, `NON AC ON SAT & HOLIDAY`, `(L)`, `EX ...` |
| Times | `HH:MM` | `HH:MM` |
| Trains | 895 | 716 in the DOWN file, numbered 90xxx–94xxx, plus 53 Harbour trains (98xxx) to Goregaon |
| Passing a station | `…` | mostly a **blank** cell; `--` in a few places |
| Row labels | consistent | mixed case, and one label letter-spaced (`N a ll a s o p a r a`) |

Station rows: Churchgate to Virar, 30 stations. Dahanu Road services are a separate PDF.

What would need doing, beyond a station table:

1. **Finding the columns.** The converter looks for train numbers in the row labelled
   `STATION`. Here they are a row lower. A modest change, but it is the core of the grid
   reader and must not disturb the Central Railway files.
2. **A different marker grammar.** Day restrictions are English phrases rather than `X` and
   `XX`, AC is spelt out, and rake length is on its own row. Each phrase needs mapping, and
   by this project's rule an unknown one rejects the train rather than being guessed at.
3. **Fast or slow from blanks.** On the main line a blank means "doesn't run on this
   section" and `…` means "passes". On the Western line a blank between two stops means
   "passes". The rule has to depend on the source. Getting it wrong mislabels trains without
   any error.
4. **Trains in both railways' files.** The 53 Harbour trains to Goregaon are in the Western
   PDF *and* in Central Railway's Harbour PDF, with the Western half of their stops in one
   and all of them in the other. One source has to win.
5. **A separate holiday rule.** Western Railway's phrases include `HOLIDAY`; whether its
   list of holidays matches Central Railway's was not checked.
6. Dadar, Bandra, Andheri and others exist on more than one line, so the shared-station
   work above is needed here too.

## 3. Live delay data for these lines

This is what decides it.

| Source | Harbour | Western |
| --- | --- | --- |
| **NTES** (collected now) | Not checked directly. On the main line NTES reports locals only beyond Kalyan, and the boards for Thane, Dadar and CSMT listed no locals at all ([`data-sources.md`](data-sources.md)). There is no reason to expect it to track Harbour locals, which run entirely inside the suburban section | Not checked. Western locals are numbered 90xxx–94xxx; NTES's train list, seen in September, had suburban numbers only in the 95xxx–99xxx range. So probably nothing |
| **Mobond / m-Indicator** (off; no permission) | In the feed, per the September spike | In the feed, per the September spike (90–94xxx) |
| Anything else | None found | None found |

So today a Harbour or Western timetable would give `/next` scheduled times and the
rule-of-thumb crowding estimate for those lines, and nothing else. No delays, no model, no
cancellations. The crowding rules were written for Kalyan–CSMT peak flows and would need
their own thinking for each line.

## 4. Estimated effort

Rough, for one person who knows this codebase. "Days" are focused working days.

| Piece | Harbour | Western |
| --- | --- | --- |
| Station table and codes, checked against NTES | 0.5 day | 0.5 day |
| Converter changes | 0.5 day (the `TNA` cells) | 3–4 days (header, markers, blank-means-pass, duplicates) |
| Stations on more than one line (schema, loader, queries, bot) | 2–3 days, **shared by both** | (shared) |
| Fixtures and tests from a few real pages | 1 day | 1.5 days |
| Bot and dashboard: choosing a line, station names that collide | 1 day, **shared** | (shared) |
| **Total** | **about 5–6 days**, of which 3–4 are the shared groundwork | **about 5–6 more days** once Harbour is done |

The Trans-Harbour and Port lines would likely be half a day each after Harbour, if their
PDFs follow the same layout. They were not opened.

## 5. Recommendation

**Don't expand yet.** In order:

1. **Keep the current line healthy first.** Real data started today. The collector, the
   health checks and the weekly retrain need a few weeks of running before anything is
   learned from them.
2. **Ask Mobond.** Its feed is the only known source of live data for Harbour and Western
   locals, and for CSMT–Kalyan locals too. Without it, expansion is a timetable viewer.
   With it, all three lines get delays at once. See "Turning Mobond on" in
   [`data-sources.md`](data-sources.md).
3. **If expanding anyway, do Harbour first.** The converter already reads its format; the
   real work is letting a station belong to more than one line, which the Western line needs
   too. It is the cheap way to find out what that change breaks.
4. **Western after that**, and only with tests built from real pages of its PDF, because
   its "blank means passes" rule can go wrong silently.

One thing worth doing regardless, and small: **find out why Python can't complete a TLS
handshake with the two railway sites on this machine**, since the dashboard's automatic
timetable download depends on it.

## What this assessment did and didn't do

- Read a saved copy of Central Railway's timetable page (no request).
- Read two pages on Western Railway's site (two requests).
- Tried to download one PDF from each site with `httpx` (two requests; both failed at the TLS
  handshake).
- Fetched the same two PDFs once each through a web fetching tool (two requests), saved them
  under `scratch/part10/`, and ran the existing converter on their first three pages.
- Did **not** open the UP files, the AC files, the Trans-Harbour, Port or Dahanu Road PDFs.
- Did **not** check NTES for Harbour or Western trains.
- Did **not** make any request to Mobond or m-Indicator.
