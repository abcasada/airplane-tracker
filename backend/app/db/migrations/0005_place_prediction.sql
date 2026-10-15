-- Per-place overrides for prediction thresholds (lead time, confidence); NULL = global defaults.
ALTER TABLE places ADD COLUMN prediction TEXT;
