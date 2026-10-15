-- One row per aircraft x place x local day it came within the place's watch radius.
-- Feeds the history fields ("rare visitor" filters).
CREATE TABLE sightings (
    hex         TEXT NOT NULL,
    place_id    INTEGER NOT NULL,
    day         TEXT NOT NULL,  -- local date, YYYY-MM-DD
    type_code   TEXT,
    first_at    REAL NOT NULL,
    last_at     REAL NOT NULL,
    min_dist_nm REAL,
    PRIMARY KEY (hex, place_id, day)
) WITHOUT ROWID;
CREATE INDEX sightings_type ON sightings(type_code, place_id, day);
CREATE INDEX sightings_day ON sightings(day);
