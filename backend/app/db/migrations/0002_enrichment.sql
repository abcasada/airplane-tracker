-- Bulk datasets are rebuilt by the dataset manager; versions live in `datasets`.
CREATE TABLE datasets (
    name         TEXT PRIMARY KEY,
    version      TEXT,
    source_url   TEXT,
    rows         INTEGER NOT NULL DEFAULT 0,
    imported_at  REAL,
    checked_at   REAL,
    last_error   TEXT
);

CREATE TABLE aircraft_db (
    icao       TEXT PRIMARY KEY,
    reg        TEXT,
    type_code  TEXT,
    flags      TEXT,
    descr      TEXT,
    year       TEXT,
    own_op     TEXT
) WITHOUT ROWID;
CREATE INDEX aircraft_db_reg ON aircraft_db(reg);
CREATE INDEX aircraft_db_type ON aircraft_db(type_code);

CREATE TABLE type_stats (
    type_code       TEXT PRIMARY KEY,
    descr           TEXT,
    airframes       INTEGER NOT NULL,
    military_share  REAL NOT NULL
) WITHOUT ROWID;

CREATE TABLE mil_ranges (
    start_hex  INTEGER NOT NULL,
    end_hex    INTEGER NOT NULL
);

CREATE TABLE plane_alert (
    icao       TEXT PRIMARY KEY,
    reg        TEXT,
    operator   TEXT,
    type_desc  TEXT,
    icao_type  TEXT,
    cmpg       TEXT,
    tag1       TEXT,
    tag2       TEXT,
    tag3       TEXT,
    category   TEXT,
    link       TEXT
) WITHOUT ROWID;

CREATE TABLE plane_alert_images (
    icao    TEXT PRIMARY KEY,
    images  TEXT NOT NULL  -- JSON list of URLs
) WITHOUT ROWID;

CREATE TABLE airlines (
    icao  TEXT PRIMARY KEY,
    name  TEXT NOT NULL,
    iata  TEXT
) WITHOUT ROWID;

CREATE TABLE military_overrides (
    key          TEXT PRIMARY KEY,  -- 'hex:ae1234' or 'reg:98-0001'
    is_military  INTEGER NOT NULL,
    note         TEXT,
    created_at   REAL NOT NULL
);
