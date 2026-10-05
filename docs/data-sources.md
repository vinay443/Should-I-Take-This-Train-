# Data sources: Mumbai suburban (Central line, around Kalyan)

Research spike, **2026-09-27**. Every "tested" note below comes from a real request made that day, between about 15:30 and 16:00 IST, from an Indian IP. That afternoon was a **Sunday with a Central line megablock**, so the delays seen in the samples are higher than on a typical weekday. The throwaway probe scripts are in `scratch/`, which is gitignored.

## TL;DR

| Need | Best source | Verdict |
|---|---|---|
| Static timetable | Central Railway's official **Pocket/Public Time Table (PTT) PDFs** on cr.indianrailways.gov.in | Works. Parsing the PDFs is the real work. |
| Live delays | **Mobond (m-Indicator) `getalllivetrains` JSON**, undocumented | Works well technically. Permission is the problem (see below). |
| Live delays, official | **NTES Live Station board** for KYN | Works, but only covers EMU legs *beyond* Kalyan, plus mail/express trains. |
| Crowding | None | No per-train crowding data exists publicly. Use proxies. |

---

## 1. Static timetables

### 1.1 Central Railway PTT PDFs (recommended)

- **Index page:** https://cr.indianrailways.gov.in/view_section.jsp?lang=0&id=0,5,2360 (Time Table → Mumbai Suburban). NTES's own "Mumbai Suburban (Central and Harbour)" menu item links to this same page.
- **Files currently linked** (all downloaded OK, `application/pdf`, text layer present, no OCR needed):

  | File | w.e.f. | Notes |
  |---|---|---|
  | `…/1728294831372-SUB%20PTT%20DN%20ML'24.pdf` | 05.10.2024 | Main line DOWN (CSMT → Kalyan → Kasara/Khopoli), 18 pages |
  | `…/1728294891897-SUB%20PTT%20UP%20ML'24.pdf` | 05.10.2024 | Main line UP |
  | `…/1770378640224-Suburban%20AC%20Services%20wef%2016.04.2025.pdf` | 16.04.2025 | AC services (main line), 6 pages |
  | `…/1786691264142-PTT%20OF%2015%20CAR%20TT%20ON%20MAINLINE%20WEF%2015%20AUG%2026.pdf` | 15.08.2026 | Which main-line services run as 15-car rakes |
  | `…/1640859089146-ABBREVIATIONS.pdf` | n/a | Service codes (A = Ambernath, K = Kalyan, KP = Khopoli…) and day markers |
  | `…/1763721416458-list%20of%20holidays%20for%20website.pdf` | n/a | Holidays that follow the Sunday schedule |
  | Harbour, Trans-Harbour and Port line PTTs | various | Not needed for Kalyan |

  (`…` = `https://cr.indianrailways.gov.in/cris//uploads/files/`)
- **What it gives:**
  - Every service as a column: the 5-digit train number (for example `96301`), a service code (`A 1`), AC and 15-car markers, and day markers.
  - One row per station with the scheduled time. `…` means the train passes without stopping, and blank means the train doesn't run on that section.
  - Day markers: **X** = not on Sunday/holiday, **XX** = not on Sat/Sun/holiday (from the abbreviations PDF).
  - **Fast/slow is not a field.** It has to be inferred from the stopping pattern (skipped stations, `…`).
- **Format and access:** static PDF over HTTPS. There is no robots.txt (`/robots.txt` returns an HTML 404 page). Government site, public information, no stated terms on reuse.
- **Update frequency:** irregular. The main-line base PTT is still the **Oct 2024** edition. Changes since then arrive as supplementary PDFs (AC services, 15-car). A new PDF gets a new URL (timestamp prefix), so a monthly job that re-scrapes the index page and diffs the link list is enough to notice changes.
- **Tested:**
  - DN and UP PDFs parse with `pdfplumber`, and I pulled 892 distinct main-line train numbers out of the column headers.
  - **All 74** CR main-line train numbers seen live in the Mobond feed (§2.1) exist in the 2024 PTT. So the numbering is stable and the live data joins to the timetable on train number.
- **Gotchas:**
  - `extract_text()` collapses blank cells, so times shift into the wrong columns. The parser has to use word x-coordinates, or table extraction with explicit column boundaries.
  - One train number can appear on more than one page.
  - Supplementary PDFs may override the base PTT.

### 1.2 Open-source projects that already parse these PDFs (use as cross-checks)

- **[56steve/rail-view](https://github.com/56steve/rail-view):** `backend/scripts/import_timetables.py` parses the CR and WR PTT PDFs with pdfplumber. It pins source URLs and SHA-256 hashes and writes `timetable.json` (~3,000 trains/day, with running days). The repo was active on 2026-09-25, but **I couldn't find a license**, so treat it as a reference for validation and don't copy code from it. Its live positions are simulated ("No authorised real-time feed for Mumbai locals is connected yet").
- **[sarththale/MumbaiSuburbanRailwayDatabaseGenerator](https://github.com/sarththale/MumbaiSuburbanRailwayDatabaseGenerator):** MIT license. Parses the official PDFs into stations/trains/train_stops (CSV/JSON). Its README doesn't mention day codes, AC or car count. Not tested.
- **[Umang-Lodaya/Mumbai-Local-TimeTable-Extractor](https://github.com/Umang-Lodaya/Mumbai-Local-TimeTable-Extractor):** scrapes trainhelp.in. That's a secondary source (see 1.4), last pushed Jan 2024.
- **[guru809/MumbaiLocalTrainsJSON](https://github.com/guru809/MumbaiLocalTrainsJSON):** last pushed 2019. Stale.

### 1.3 NTES train schedule: not usable for the core section

`enquiry.indianrail.gov.in/mntes` does know about suburban trains: its train list includes 443 numbers in the 95xxx–99xxx range. But for Central line EMUs it mostly has **only the leg beyond Kalyan**.

- **Tested:** NTES has `96301` (A1, which per the PTT leaves CSMT at 00:02) as "KALYAN JN → AMBARNATH, 01:30–01:46, 4 stops, Type: SUBURBAN". erail.in, RailRadar and the unofficial [Indian Railways GTFS](https://github.com/Neo2308/indianrailways-gtfs) (Mobility Database `mdb-2867`) all mirror this truncated NTES data.
- A few services are present end to end (for example AC local `95324` ABH→CSMT).

### 1.4 Dead ends and weak sources for timetables

| Source | Result |
|---|---|
| Chalo BEST GTFS `gtfs.chalobest.in/gtfs_mumbai_railways_20140101.zip` (from the [DataMeet thread](https://groups.google.com/g/datameet/c/B5HoyDcLcrw)) | **Dead.** DNS no longer resolves, and the data was from 2014 anyway. |
| transportformumbai.com/central_complete_time_table.php | **Dead.** Returns 404. |
| data.gov.in "Indian Railways Time Table" | Covers reservable (long-distance) trains only. data.gov.in's robots.txt is `Disallow: /` and the pages render client-side, so the only sanctioned route is the keyed API. |
| Kaggle [prasad22/mumbai-local-train-dataset](https://www.kaggle.com/datasets/prasad22/mumbai-local-train-dataset) | Station metadata only (station, line, distance, inter-station minutes). **No timetable.** Built from Wikipedia and m-Indicator. |
| [trainhelp.in timetable page](https://www.trainhelp.in/mumbai-local-train-time-table/) | HTML tables (~1,800 rows), updated 2026-08-20. It transcribes the PDFs and **has visible errors** (for example "Ending at at 07.03", and 96001 SKP1 listed as Karjat → Thane). Its robots.txt allows crawling. Use only as a fallback. |
| Mobility Database catalog (full CSV grep) | No Mumbai suburban rail GTFS, official or unofficial. Only BEST bus (`mdb-3138`). |
| Google Maps / Yatri GTFS | Google Maps shows Mumbai locals (via the Yatri partnership, 2025), but that GTFS is not published. |

---

## 2. Live running status and delays

### 2.1 Mobond / m-Indicator live map: best data, no permission

- **URL:** `GET https://mobond.com/mtracker/getalllivetrains`. I found it in [kevinnadar22/mindicatormcp](https://github.com/kevinnadar22/mindicatormcp) (`app/core/config.py`).
- **Access:** no auth and no special headers. It returns about 2.3 KB of JSON. It's hosted on Google Frontend (App Engine), so it should be reachable from GitHub runners.
- **Format:** a flat object of `train_no → status string`:

  ```json
  "95222": "At ULHAS NAGAR, 46 min Late",
  "95413": "Between SHAHAD - AMBIVLI, 21 min Late",
  "97239": "Reaching CSMT (at VIDYAVIHAR now), 39 min Late",
  "95229": "[7 min ago] Between AMBARNATH - BADLAPUR, 36 min Late (Less Accurate)",
  "95218": "Cancellation Reported"
  ```

  There are about 20 string shapes (`At X`, `Between X - Y`, `Crossed X`, `Arriving X`, `Reaching X (at Y now)`, `Rake at X`, optional `N min Late/Early`, optional `[N min ago]` staleness prefix, optional `(Less Accurate)`). A regex parser covers all of them.
- **Coverage (tested):**
  - 213, then 211, then 208 trains across three snapshots.
  - About 72–74 of them were CR main-line numbers (95/96/97xxx), with positions across the **whole corridor, including the core** (Matunga, Sion, Dadar, Thane, Kalyan…).
  - Rough check: ~900 main-line services a day at 60–90 min per trip means roughly 50–70 running at once, so this looks close to complete for what's running. That's an estimate, not a measurement.
  - It also includes WR (90–94xxx), Harbour and Pune locals.
  - It flags **cancellations**, which NTES does not. 9 were flagged on megablock Sunday.
  - About 40% of entries carry "(Less Accurate)". My guess is these are crowdsourced from app users, but the underlying source is **undocumented**.
- **Update frequency (tested):** 126 of 213 entries changed between two fetches ~2 minutes apart. Near real-time. It is a **snapshot of trains running right now**, with no history and no per-station timetable.
- **Joins to the timetable:** yes. Train numbers match the PTT (74/74).
- **Terms and robots:**
  - `mobond.com/robots.txt` is 404 (no rules).
  - The [m-Indicator terms](http://m.mobond.com/terms.xhtml) (updated 2025-10-27) say "All copyrights and trademarks are the property of Mobond" and forbid misusing the app or disrupting services. They don't mention APIs or scraping.
  - This is a **private, undocumented endpoint of a commercial app**. It can change or disappear without notice, and using it without asking is a grey area.
- **Responsible use if adopted:**
  - At most 1 request every 15–30 minutes (≤96/day, ~220 KB/day).
  - Send an honest User-Agent with a repo URL.
  - Store derived observations, not a public mirror of the raw feed.
  - **Email Mobond and ask for permission first.**

#### Turning Mobond on (a checklist for when permission arrives)

**As of 2026-10-05 Mobond has not been asked and has not agreed. The adapter is off, and no
request has been made to Mobond or m-Indicator by this project's code since the research
spike above.** Nothing below should be done until step 1 is complete.

The adapter ([`src/sitt/ingest/live/mobond.py`](../src/sitt/ingest/live/mobond.py)) is built
so that switching it on is a deliberate act and polling stays gentle.

**1. Permission**

- [ ] Write to Mobond (the m-Indicator team) and ask. Say what the project is (personal,
      non-commercial), what you want to fetch (`getalllivetrains`, once every 15 minutes or
      slower), what you will store (derived fields: train number, station, delay) and what
      you won't publish (their status text).
- [ ] Get a reply that says yes, **in writing**. Keep it. No reply is not a yes.
- [ ] Note any conditions they set: a slower rate, attribution wording, a different endpoint,
      a contact address to put in the User-Agent.

**2. Terms**

- [ ] Re-read the [m-Indicator terms](http://m.mobond.com/terms.xhtml). They were last
      checked on 2026-09-27 and may have changed.
- [ ] Confirm that what you were allowed covers publishing derived data, if you intend to
      push Parquet files to the public `data` branch. If it doesn't, keep the data local.
- [ ] `raw_status` (their text) never leaves your machine: it is not in the Parquet files or
      the exports, and tests enforce that. Don't weaken it. Leave `SITT_COMMIT_RAW` alone.

**3. Attribution**

- [ ] Add a line to the README, and to anything public built on the data, naming Mobond /
      m-Indicator as the source of live running status, in the wording they ask for.
- [ ] If they gave a contact address or want one, set `SITT_USER_AGENT` to a string that
      includes it. The default already names the project, the request rate and the
      repository URL.

**4. Request budget**

- [ ] Decide the rate. The default and the minimum is one request per 15 minutes, which is 96
      a day and about 220 KB. If they asked for slower, set
      `SITT_MOBOND_MIN_INTERVAL_MINUTES` (it can only make the gap longer, never shorter).
- [ ] Collect from **one place only**: either this PC or the GitHub workflow, never both.
      Each keeps its own record of when it last asked, so two collectors would double the
      rate.
- [ ] On GitHub the rate limit has no memory between runs, because each run starts from a
      fresh checkout. There the cron schedule is the only limit. Don't make it more frequent
      than every 15 minutes.

**5. Switch it on**

Both settings must be exactly `true`. One alone does nothing.

```
SITT_MOBOND_ENABLED=true
SITT_MOBOND_PERMISSION_CONFIRMED=true
```

- [ ] Locally: add both to `.env`. On GitHub: add both as repository variables (Settings →
      Secrets and variables → Actions → Variables).
- [ ] Run `uv run sitt-collect` once by hand and read the log. Expect one line for Mobond
      with a few hundred observations.
- [ ] Run it again straight away. Expect "Mobond not polled (rate limit)". That is the rate
      limit working.

**6. What to watch afterwards**

In `uv run sitt-health` ([`collector.md`](collector.md#8-the-health-report)):

- [ ] The `mobond` line says **UP**, with readings in the low hundreds per run. Runs where it
      was rate-limited or backing off are counted with the switched-off runs, not as
      failures.
- [ ] No `source_down:mobond` or `low_readings:mobond` alert. A string of failures means
      they have blocked or changed the endpoint: **stop and ask, don't work around it.** The
      adapter backs off by itself, doubling the wait after each failure up to a day.
- [ ] Trains matched against unmatched. Central line numbers (95xxx–97xxx) should match the
      timetable. Western and Harbour trains are in the feed too and won't.

In `uv run sitt-dq` ([`data-quality.md`](data-quality.md)):

- [ ] **Station codes not in the timetable.** These are Mobond station names the alias map
      (`src/sitt/ingest/live/stations.py`) doesn't know. Add them.
- [ ] The **less accurate** rate for `mobond`. About 40% was seen in the spike.
- [ ] `delay_jump` and `implausible_delay` flags on Mobond rows.

And once, in the database:

```sql
SELECT count(*) FROM observations WHERE source = 'mobond' AND event = 'unknown';
```

- [ ] Any rows here are status wordings the parser doesn't know. Look at them in the raw
      archive (`data/raw/`) and extend the parser. **The test fixture for this adapter is
      hand-written and may be inaccurate** (see
      [`tests/fixtures/live/README.md`](../tests/fixtures/live/README.md)), so the first real
      responses are the first real test of the parser.

**To switch it off again:** set either variable to anything but `true`. No request is made
from then on.

How the adapter behaves:

| | |
| --- | --- |
| Opt-in | Two switches, both exactly `true`, checked inside the fetch function itself, so no caller can skip the check |
| Rate limit | One request per `SITT_MOBOND_MIN_INTERVAL_MINUTES`, floor 15. The time of the last request is in `data/logs/mobond_state.json`, written before the request goes out |
| Backoff | Each failed request doubles the wait: 30 min, 1 h, 2 h, ... up to 24 h. One success resets it |
| Timeouts | 10 seconds, 5 to connect. One attempt, no retries |
| User-Agent | `should-i-take-this-train/0.1 (personal non-commercial research project; at most one request every 15 minutes; +https://github.com/vinay443/Should-I-Take-This-Train-)` |
| If the state file is unreadable | Treated as "a request was just made": it waits one interval |
| If the request time can't be recorded | No request is made |

### 2.2 NTES (official, CRIS): works, but only near Kalyan and beyond

- **URL:** `https://enquiry.indianrail.gov.in/mntes/`, the mobile web UI.
- **Access:** HTML, no captcha on these screens. The captcha is only on the feedback form. The flow is:
  1. `GET /mntes/` to get cookies.
  2. `GET /mntes/GetCSRFToken?t=<ms>`, which returns `<input type='hidden' name='<random>' value='<random>'>`.
  3. `POST` the form with that token field added:
     - **Live Station:** `q?opt=LiveStation&subOpt=show` with `jFromStationInput=KYN`, `nHr=2|4|8`, `lan=en`, `appLang=en`, `jToStationInput=`, `jStnName=`, `jStation=`.
     - **Train running:** `tr?opt=TrainRunning&subOpt=FindRunningInstance` with `trainNo`, `jDate`, `lan=en`. It returns the last 4 start dates.

  That's 3 requests and ~300 KB of HTML per board. It worked headlessly with plain `urllib` (`scratch/ntes_livestation.py`).
- **Tested, KYN 2-hour board:**
  - 65 trains. Most are EMUs **originating or terminating at Kalyan for the outer legs** (KYN–BUD, KYN–TLA, KYN–KSRA, KYN–KJT, KYN–ASO), each with a live expected time and delay, for example `95222 BUD-KYN … 41 Mins` and `95413 KYN-KSRA … 21 Mins`. Mail/express trains through Kalyan are also listed with delays.
  - The **Thane** board had only 18 trains in 2 hours, and just 1 main-line EMU (the AC local 95324). NTES does not track core-section (CSMT–Kalyan) locals.
- **Data quality:**
  - Intermediate stations are usually `--` (not reported). Actuals appear only at a few reporting points.
  - For the AC local 95324, NTES showed 24–26 min late at Kalyan, then "1 min" at Kurla and on time at CSMT, several days in a row. That's physically implausible and probably a default fill.
  - At 15:50 NTES said today's 95324 was "Yet to start" while Mobond had it between Thane and Mulund, 7 min late.
  - Treat NTES **core-section** EMU data as unreliable.
- **Useful for this project anyway:**
  1. Outer-leg EMU delays around Kalyan (Titwala, Badlapur, Kasara, Karjat, Ambernath).
  2. **Mail/express delays at Kalyan, Thane and Dadar as a corridor-health signal.** They share tracks with the fast locals.
  3. It gives an official source to fall back on if Mobond disappears.
- **Terms and robots:** there's no usable robots.txt; the request to the root host returns nothing. NTES is a public government service, but it has no API terms, and bulk automated use isn't sanctioned. [railpull](https://github.com/shwetankg07/railpull), an NTES crawler, keeps to about 1 request every 1.2 s and warns that bulk redistribution "may run against the operator's terms".
- **Unverified risk:** GitHub-hosted runners are US/Azure IPs. Indian Railways/CRIS sites sometimes block foreign IPs, and I only tested from an Indian IP. **The first implementation step should be a one-off `workflow_dispatch` run that fetches one NTES board and the Mobond feed from a GitHub runner.**

### 2.3 Other live candidates

| Source | Tested? | Result |
|---|---|---|
| **RailRadar** ([railradar.in](https://railradar.in/), [API docs](https://railradar.in/docs)) | Yes: web pages and API without a key | robots.txt `Allow: /`. It lists 3,179 "Mumbai local" trains, but the schedules are the truncated NTES ones (96301 = Kalyan→Ambarnath only). Its local "live" status comes from **its own users' crowdsourced GPS** ("offline" when nobody is sharing). The API (`api.railradar.in/v1`, `/lookup/trains/local`) returns 401 without a key. The free tier is 1,000 requests/month, which is **less than polling one endpoint every 30 min (≈1,440/month)**. Paid tier prices weren't visible without JS. Not worth it for this use. |
| **Yatri** (official CR/WR partner app, CDP Yatri Pvt Ltd) | Web only | The app has live GPS positions (a GPS unit in every rake, ~15 s refresh), but there is **no public API**. Its [terms](https://yatrirailways.com/terms-of-use) forbid attempting to extract the app's source, which rules out APK reverse-engineering. The web page [live-railway-announcements](https://yatrirailways.com/live-railway-announcements) is server-rendered HTML (Bludit CMS, robots allows it) with cards for Mega Blocks, Cancellations, Delays and Monsoon. It only showed **2 summary cards** (both megablock notices) that link into the app. At best it gives a coarse "is there a disruption" flag. |
| Google Maps live trains (via Yatri) | No | There's no API that exposes it. The Routes/Directions API is paid and its terms forbid storing results. **Dead end.** |
| Where Is My Train (Google) | No | Works on-device with no API. **Dead end.** |
| erail.in, ixigo, RailYatri, ConfirmTkt, Trainman | erail tested | They mirror NTES and inherit its coverage gap. erail's robots.txt only blocks the availability endpoint. There's no gain over NTES directly. |
| X/Twitter @Central_Railway, @drmmumbaicr | No | Official disruption posts, but API access is paid, and the scraping options (Nitter etc.) are gone. **Not viable** for a free Actions job. |
| [lakshyakurup/mumbai-local-delay-tracker](https://github.com/lakshyakurup/mumbai-local-delay-tracker) | README | **Mock data only** (`scraper/mock_scraper.py`). |

### 2.4 Planned disruptions (megablocks)

Megablocks are announced 1–2 days ahead: on X, on the Yatri announcements page, and on third-party pages like trainhelp.in's [megablock page](https://www.trainhelp.in/p/mumbai-mega-block.html). I didn't find a clean, structured official feed. A daily scrape of the Yatri page is the cheapest way to get a flag that a block is scheduled today.

---

## 3. Crowding

**There is no public per-train or per-coach crowding data.** Here is what I checked:

- **Yatri crowd prediction:** announced 2025-06-28 as "coming soon". It would be crowdsourced (green/yellow/red from user input) and in-app only, with no API.
- **m-Indicator:** has a user chat, but no structured crowd field.
- **data.gov.in:** "Railway-wise details of the number of daily train services and passengers in suburban service, 2021-22" is an aggregate per zonal railway, useless per train. The page didn't render in a browser, and its robots.txt disallows crawling.
- **Station footfall:** only scattered figures exist, in Wikipedia and Yatri blog posts (for example Kalyan at ~360k passengers/day), plus MRVC survey reports (PDF studies, not a feed).
- **[RailMitra](https://github.com/Nishant19206/RailMitra-Real-Time-Coach-Crowd-Detection-for-Mumbai-s-Suburban-Trains):** a student computer-vision prototype. Not a data source.

**Usable proxies,** all derivable from the sources above:

- Time of day × direction (towards CSMT in the morning peak, away in the evening).
- Fast vs slow.
- **12- vs 15-car rake** (the 15-car PDF).
- AC vs non-AC (the AC PDF, which also marks AC services in the PTT).
- Whether the train starts at Kalyan (you get a seat) or passes through.
- **Cancellation or heavy delay of the preceding service(s)** on the same pattern. The Mobond feed reports cancellations, and a delayed train following a gap fills up.
- Megablock day or monsoon disruption.

This will be a modelled estimate, never a measurement, and the project should say so in the UI.

---

## 4. Recommendation

### Static timetable: CR official PTT PDFs

1. Parse the main-line DN and UP PTT with pdfplumber, using word coordinates. Apply the AC-services and 15-car supplements, the X/XX day codes and the Sunday-schedule holidays. Derive fast/slow from the stopping pattern.
2. Validate the output against rail-view's `timetable.json` and/or sarththale's generator (spot-check train counts at KYN, first and last trains).
3. Run a monthly Actions job that re-reads the CR index page and fails loudly when the PDF link set changes.

### Live data for a 15–30 minute GitHub Actions poll: Mobond `getalllivetrains`, with NTES KYN as the official companion

- **Mobond:** one GET per run (~2.3 KB). It covers the whole corridor, includes cancellations, and joins to the PTT on train number. Parse each status string into `(train_no, observed_at, position, delay_min, accuracy_flag, cancelled)` and append it to a compact CSV/Parquet.
  - Caveats: it's a private endpoint of a commercial app and could disappear.
  - **Before relying on it, ask Mobond for permission.** Keep to 1 request per run, use an identifying User-Agent, and don't republish the raw feed.
- **NTES KYN Live Station (`nHr=2`):** 3 requests per run (~300 KB). Use it for outer-leg EMUs and mail/express corridor delays, and for cross-checking Mobond. Keep it at one station, or two at most (KYN, maybe TNA for express trains only), with no parallel requests.
- Both are snapshots, so a 15–30 min cadence gives a few observations per trip. That's enough for delay distributions by time and day, not for second-by-second tracking.
- Actions caveats:
  - Scheduled workflows run late or get skipped under load.
  - GitHub disables them after 60 days without repo activity.
  - Runner IPs are outside India, so **verify NTES reachability first.**

### Fallback if no live source is viable (Mobond refuses or blocks, and NTES blocks runners)

1. **Schedule plus historical priors.** Use whatever delay history was collected before the source went away (even a few weeks' worth), grouped by service, time band, weekday/Sunday, and monsoon month. Serve "typical delay for this service", not live status.
2. **Disruption flag.** A daily scrape of the Yatri announcements page and CR megablock notices marks known-bad days.
3. **Run the NTES poll from somewhere with an Indian IP** (a small VPS or a home machine) instead of GitHub-hosted runners, if the problem is only geo-blocking.
4. **User reports:** let users submit "my train was N min late". That's slow to build up, but it's legitimately your own data.
5. Treat crowding as a modelled estimate from the proxies in §3 either way.

---

## Appendix: scratch scripts (gitignored)

- `scratch/mobond_probe.py`: snapshot and summarise the Mobond feed.
- `scratch/ntes_probe.py`: NTES session and CSRF helper. Usage: `python ntes_probe.py sched|run|"<path>" <trainNo…>`.
- `scratch/ntes_livestation.py`: headless NTES Live Station board fetch.
- `scratch/rv_import.py`: copy of rail-view's importer, read for its PDF URL list (not reused).
- Sample outputs (`*.pdf`, `ntes_*.html`, `mobond_live_t*.json`) are next to them.
