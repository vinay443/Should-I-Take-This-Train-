-- Schema for the Should-I-Take-This-Train DuckDB database.
--
-- Conventions:
--   * Every statement is idempotent (IF NOT EXISTS), so applying this file to an
--     existing database is safe. To extend the schema, add new tables or columns
--     here in the same style (new columns should be nullable or have a DEFAULT).
--   * Instants (when something was observed or reported) are TIMESTAMPTZ.
--     Timetable times are TIME values in Mumbai local time (Asia/Kolkata).
--   * Station and train identifiers are short text codes, not surrogate integers,
--     so rows stay readable and ingest scripts can upsert by natural key.


-- Stations on a suburban line, e.g. ('KYN', 'Kalyan', 'central', 30).
-- `seq` orders stations along the line, starting at the CSMT end, and is used to
-- work out direction and distance between stops. A station on more than one line
-- (e.g. Thane) currently takes the line we care about. If we need more than one,
-- split this into stations + station_lines.
CREATE TABLE IF NOT EXISTS stations (
    code    VARCHAR PRIMARY KEY,
    name    VARCHAR NOT NULL,
    line    VARCHAR NOT NULL,
    seq     INTEGER NOT NULL
);


-- A single scheduled service, e.g. the 08:12 Kalyan -> CSMT fast.
-- `train_id` is our stable internal key. `number` is the public train or
-- timetable number, which is not always unique across timetable revisions.
-- `label` is the destination board text riders recognise ("CSMT", "Kalyan").
-- `direction` follows the railway convention: 'up' runs toward CSMT and 'down'
-- runs away from it.
CREATE TABLE IF NOT EXISTS trains (
    train_id    VARCHAR PRIMARY KEY,
    number      VARCHAR,
    label       VARCHAR NOT NULL,
    train_type  VARCHAR NOT NULL CHECK (train_type IN ('fast', 'slow')),
    line        VARCHAR NOT NULL,
    direction   VARCHAR NOT NULL CHECK (direction IN ('up', 'down'))
);

-- Optional train attributes, filled by the timetable loader when the CSV has them
-- (see docs/timetable-format.md). NULL means the source didn't say.
--   `service_code`       the timetable's own code for the service, e.g. 'A 1' (the first
--                        Ambernath local) or 'K 28'.
--   `is_ac`              an air-conditioned rake.
--   `car_count`          rake length in cars (12 or 15), where the source gives it.
--   `is_ladies_special`  a train reserved for women.
--   `ac_weekdays_only`   an AC train that runs without AC on Saturdays, Sundays and
--                        nominated holidays ("AC#" in the timetable). Only meaningful
--                        when `is_ac` is true.
ALTER TABLE trains ADD COLUMN IF NOT EXISTS service_code VARCHAR;
ALTER TABLE trains ADD COLUMN IF NOT EXISTS is_ac BOOLEAN;
ALTER TABLE trains ADD COLUMN IF NOT EXISTS car_count INTEGER;
ALTER TABLE trains ADD COLUMN IF NOT EXISTS is_ladies_special BOOLEAN;
ALTER TABLE trains ADD COLUMN IF NOT EXISTS ac_weekdays_only BOOLEAN;


-- The timetable: where and when each train is meant to stop.
-- `stop_seq` orders the stops within a trip. Use it rather than sorting by time,
-- because times wrap around midnight on late-night services.
-- Arrival is NULL at the origin and departure is NULL at the terminus.
-- `days_of_operation` is a 7-character Monday-to-Sunday mask, e.g. 'YYYYYYN'
-- for a train that runs every day except Sunday.
CREATE TABLE IF NOT EXISTS scheduled_stops (
    train_id            VARCHAR NOT NULL REFERENCES trains (train_id),
    station_code        VARCHAR NOT NULL REFERENCES stations (code),
    stop_seq            INTEGER NOT NULL,
    scheduled_arrival   TIME,
    scheduled_departure TIME,
    days_of_operation   VARCHAR NOT NULL DEFAULT 'YYYYYYY'
                        CHECK (regexp_full_match(days_of_operation, '[YN]{7}')),
    PRIMARY KEY (train_id, station_code)
);


-- Timing data for a train at a station, collected from live sources.
-- Each row is one reading as captured, so repeated polls of the same train
-- produce multiple rows. `time_kind` says whether `actual_or_expected_time` is a
-- confirmed arrival ('actual') or a running-status prediction ('expected').
-- `delay_minutes` is relative to the timetable and is negative when early.
-- There are deliberately no foreign keys here: collectors should never drop data
-- just because the reference tables have not caught up with a new train or station.
CREATE SEQUENCE IF NOT EXISTS observations_id_seq;
CREATE TABLE IF NOT EXISTS observations (
    id                      BIGINT PRIMARY KEY DEFAULT nextval('observations_id_seq'),
    observed_at             TIMESTAMPTZ NOT NULL,
    train_id                VARCHAR NOT NULL,
    station_code            VARCHAR NOT NULL,
    actual_or_expected_time TIMESTAMPTZ,
    time_kind               VARCHAR CHECK (time_kind IN ('actual', 'expected')),
    delay_minutes           DOUBLE,
    source                  VARCHAR NOT NULL
);

-- Columns added for the live collector (src/sitt/ingest/live/). ADD COLUMN IF NOT EXISTS
-- keeps this re-runnable on databases created before they existed.
--   `train_number`  the number exactly as the source gave it. `train_id` holds the matched
--                   trains.train_id, or this raw number until a unique match is found
--                   (`python -m sitt.ingest.live.load` re-matches after each load).
--   `event`         what the reading describes at `station_code`:
--                   NTES: 'arrival' | 'departure'.
--                   Mobond: 'at' | 'arriving' | 'crossed' | 'between' (station_code is the
--                   station last passed) | 'rake_at' (rake waiting, not yet running) |
--                   'cancelled' | 'unknown' (status text not understood; see the raw archive).
--                   station_code is an IR code where known, else the source's station name,
--                   or '' when the source gives no station (cancellations).
--   `cancelled`     the source reports this service as cancelled.
--   `less_accurate` Mobond's "(Less Accurate)" marker, on roughly 40% of its readings.
--   `raw_status`    the source's text for this train, e.g. Mobond's status string. The
--                   collector's Parquet files leave it out, because they are committed to
--                   a public branch, so it is NULL for rows loaded from them. The text
--                   survives only in the raw archive (data/raw/, or workflow artifacts).
--   `batch_id`      the collector run that produced the row, which is also its Parquet file name.
ALTER TABLE observations ADD COLUMN IF NOT EXISTS train_number VARCHAR;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS event VARCHAR;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS cancelled BOOLEAN DEFAULT false;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS less_accurate BOOLEAN DEFAULT false;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS raw_status VARCHAR;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS batch_id VARCHAR;


-- Suspect observations, as marked by `sitt-dq --write-flags` (sitt.dq, docs/data-quality.md).
-- Nothing is ever deleted from `observations`; a doubtful reading gets a row here instead,
-- and the feature builder leaves flagged readings out by default. This table is derived:
-- it is recomputed from `observations` each time flags are written.
--   `flag`    'exact_duplicate' | 'near_duplicate' | 'implausible_delay' | 'delay_jump' |
--             'schedule_mismatch' | 'far_from_schedule' | 'future_timestamp' |
--             'batch_time_mismatch'
--   `detail`  the numbers behind the flag, e.g. 'delay 412 min, limit 180'.
CREATE TABLE IF NOT EXISTS dq_flags (
    observation_id  BIGINT NOT NULL,
    flag            VARCHAR NOT NULL,
    detail          VARCHAR,
    flagged_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (observation_id, flag)
);


-- One row per collector run per source, written by `sitt-collect` (sitt.ingest.live.runlog).
-- A run that fetched nothing, or failed outright, still gets its rows, so a hole in the
-- data can be told apart: no row at all means no run happened (machine off or asleep),
-- a 'failed' row means the run happened and the source didn't answer.
--   `run_id`            the collector's batch ID, e.g. '20261005T144504Z-a0f215'. Equal to
--                       observations.batch_id for the readings the run produced.
--   `status`            'ok'               fetched, parsed and loaded;
--                       'partial'          fetched, but nothing was parsed or the readings
--                                          could not be loaded into this database yet;
--                       'failed'           the source could not be fetched or parsed;
--                       'skipped_disabled' the source is switched off (Mobond by default).
--   `readings`          rows the source produced in this run.
--   `trains_matched`,   distinct train numbers among them that are, or are not, in the
--   `trains_unmatched`  timetable (`trains`). NULL when the run loaded nothing.
--   `error`             a short description of what went wrong. Never a response body.
--   `host`              the machine that ran it.
--   `backfilled`        true for rows reconstructed from `observations` for runs made
--                       before this table existed. Their times come from the batch ID.
CREATE TABLE IF NOT EXISTS collector_runs (
    run_id              VARCHAR NOT NULL,
    source              VARCHAR NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL,
    finished_at         TIMESTAMPTZ,
    status              VARCHAR NOT NULL
                        CHECK (status IN ('ok', 'partial', 'failed', 'skipped_disabled')),
    readings            INTEGER NOT NULL DEFAULT 0,
    trains_matched      INTEGER,
    trains_unmatched    INTEGER,
    error               VARCHAR,
    host                VARCHAR,
    backfilled          BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (run_id, source)
);


-- What `sitt-health --alert-only` last said about each problem, so the same problem
-- isn't reported again on every check (see sitt.health and docs/collector.md).
--   `alert_key`     the problem, e.g. 'no_successful_run' or 'source_down:ntes'.
--   `active`        whether the problem was present at the last check.
--   `first_seen_at` when the current episode started.
--   `last_sent_at`  when a message about it was last sent.
CREATE TABLE IF NOT EXISTS alert_state (
    alert_key       VARCHAR PRIMARY KEY,
    active          BOOLEAN NOT NULL,
    first_seen_at   TIMESTAMPTZ NOT NULL,
    last_sent_at    TIMESTAMPTZ,
    last_message    VARCHAR
);


-- How crowded a train was, as reported by riders (e.g. through the Telegram bot).
-- Riders often don't know the train_id, so a report may carry only a free-text
-- `train_description` ("8:12 fast from Kalyan"), which is resolved to a train_id
-- later. At least one of the two must be present.
-- `crowd_level` runs from 1 (empty) to 5 (can't board).
-- Bot reports use `source` = 'telegram:<user id>' and keep the raw message in `note`.
CREATE SEQUENCE IF NOT EXISTS crowd_reports_id_seq;
CREATE TABLE IF NOT EXISTS crowd_reports (
    id                  BIGINT PRIMARY KEY DEFAULT nextval('crowd_reports_id_seq'),
    reported_at         TIMESTAMPTZ NOT NULL,
    train_id            VARCHAR,
    train_description   VARCHAR,
    station_code        VARCHAR,
    crowd_level         TINYINT NOT NULL CHECK (crowd_level BETWEEN 1 AND 5),
    source              VARCHAR NOT NULL,
    note                VARCHAR,
    CHECK (train_id IS NOT NULL OR train_description IS NOT NULL)
);


-- Planned engineering blocks ("megablocks"): a section of line closed for maintenance
-- for a few hours, usually on a Sunday, which delays, diverts or cancels trains.
-- Filled by sitt.ingest.blocks (announcements or manual entry) and, in a synthetic
-- database, by sitt.synth.
--   `block_id`      stable key built from the date, line, section, times and tracks, so
--                   re-ingesting the same announcement doesn't add a second row.
--   `block_date`    the day the block starts (Mumbai local date).
--   `line`          'central' (main line), 'harbour', 'transharbour' or 'western'.
--   `from_station`, `to_station`  the ends of the section: station codes where the
--                   station is known, else the name as announced. NULL if not stated.
--   `start_time`, `end_time`      Mumbai local time. An end before the start means the
--                   block runs past midnight. NULL if not stated.
--   `tracks`        'fast', 'slow' or 'both'; NULL if not stated.
--   `direction`     'up', 'down' or 'both'; NULL if not stated.
--   `source`        'yatri', 'manual' or 'synthetic'.
--   `summary`       a short description in our own words, never the announcement's text.
CREATE TABLE IF NOT EXISTS blocks (
    block_id        VARCHAR PRIMARY KEY,
    block_date      DATE NOT NULL,
    line            VARCHAR NOT NULL,
    from_station    VARCHAR,
    to_station      VARCHAR,
    start_time      TIME,
    end_time        TIME,
    tracks          VARCHAR CHECK (tracks IN ('fast', 'slow', 'both')),
    direction       VARCHAR CHECK (direction IN ('up', 'down', 'both')),
    source          VARCHAR NOT NULL,
    summary         VARCHAR,
    recorded_at     TIMESTAMPTZ
);
