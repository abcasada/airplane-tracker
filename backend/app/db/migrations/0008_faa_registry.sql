-- FAA Releasable Aircraft Database (MASTER + ACFTREF), rebuilt by the dataset manager.
-- Only the columns the app uses; street addresses are never imported.
CREATE TABLE faa_registry (
    icao             TEXT PRIMARY KEY,  -- Mode S hex, lowercase
    n_number         TEXT NOT NULL,     -- with the leading N, e.g. N12345
    mfr_mdl_code     TEXT,
    year             TEXT,
    registrant_type  TEXT,              -- FAA code: 1 individual … 5 government …
    name             TEXT,
    status           TEXT,
    type_aircraft    TEXT,
    type_engine      TEXT
) WITHOUT ROWID;
CREATE INDEX faa_registry_n ON faa_registry(n_number);

CREATE TABLE faa_aircraft_ref (
    code           TEXT PRIMARY KEY,
    mfr            TEXT,
    model          TEXT,
    engines        INTEGER,
    seats          INTEGER,
    weight_class   TEXT
) WITHOUT ROWID;
