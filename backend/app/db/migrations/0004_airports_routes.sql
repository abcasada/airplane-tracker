CREATE TABLE airports (
    ident         TEXT PRIMARY KEY,
    icao          TEXT,
    iata          TEXT,
    name          TEXT NOT NULL,
    type          TEXT NOT NULL,
    lat           REAL NOT NULL,
    lon           REAL NOT NULL,
    elevation_ft  REAL,
    municipality  TEXT,
    country       TEXT
) WITHOUT ROWID;
CREATE INDEX airports_icao ON airports(icao);
CREATE INDEX airports_iata ON airports(iata);

CREATE TABLE runways (
    airport_ident  TEXT NOT NULL,
    le_ident       TEXT,
    he_ident       TEXT,
    length_ft      REAL,
    le_lat         REAL,
    le_lon         REAL,
    le_heading     REAL,
    he_lat         REAL,
    he_lon         REAL,
    he_heading     REAL
);
CREATE INDEX runways_airport ON runways(airport_ident);

CREATE TABLE routes (
    callsign    TEXT PRIMARY KEY,
    legs        TEXT,          -- JSON list of {icao, iata, name, lat, lon}; NULL when unknown
    plausible   INTEGER,
    source      TEXT NOT NULL,
    fetched_at  REAL NOT NULL
) WITHOUT ROWID;

CREATE TABLE cache (
    namespace   TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT,
    fetched_at  REAL NOT NULL,
    PRIMARY KEY (namespace, key)
) WITHOUT ROWID;
