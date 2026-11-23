\set ON_ERROR_STOP on

BEGIN;

-- 자동 탑승 추정에는 짧은 GPS 궤적만 보관한다. 이동 삭제 시 함께 삭제된다.
CREATE TABLE IF NOT EXISTS app.journey_location_samples (
    journey_id UUID NOT NULL REFERENCES app.journeys(id) ON DELETE CASCADE,
    observed_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    location extensions.geography(Point,4326) NOT NULL,
    accuracy_m DOUBLE PRECISION,
    PRIMARY KEY (journey_id, observed_at),
    CHECK (accuracy_m IS NULL OR (accuracy_m >= 0 AND accuracy_m < 'Infinity'::double precision))
);
CREATE INDEX IF NOT EXISTS journey_location_samples_recent_idx
    ON app.journey_location_samples(journey_id, observed_at DESC);

COMMIT;
