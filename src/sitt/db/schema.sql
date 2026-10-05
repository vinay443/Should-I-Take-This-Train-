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
