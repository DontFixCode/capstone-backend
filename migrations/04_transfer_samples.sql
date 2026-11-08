\set ON_ERROR_STOP on

BEGIN;

CREATE TABLE app.transfer_samples (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    sample_key TEXT NOT NULL UNIQUE,
    cohort_key TEXT NOT NULL,
    independence_group TEXT NOT NULL,

    evaluated_at TIMESTAMPTZ NOT NULL,
    front_prediction_at TIMESTAMPTZ NOT NULL,
    next_prediction_at TIMESTAMPTZ NOT NULL,

    front_stop_id BIGINT
        REFERENCES app.stops(id),
    next_stop_id BIGINT
        REFERENCES app.stops(id),

    walk_seconds DOUBLE PRECISION NOT NULL,
    buffer_seconds DOUBLE PRECISION NOT NULL,

    prediction_context JSONB NOT NULL,

    front_event_observation_id BIGINT
        REFERENCES app.transit_observations(id),
    next_event_observation_id BIGINT
        REFERENCES app.transit_observations(id),

    front_observed_at TIMESTAMPTZ,
    next_observed_at TIMESTAMPTZ,

    observation_context JSONB NOT NULL DEFAULT '{}'::jsonb,

    label_status TEXT NOT NULL DEFAULT 'pending',
    time_feasible BOOLEAN,

    predicted_probability NUMERIC(8,6),
    probability_context JSONB NOT NULL DEFAULT '{}'::jsonb,

    label_deadline_at TIMESTAMPTZ NOT NULL,
    labeled_at TIMESTAMPTZ,

    CONSTRAINT transfer_samples_label_status_check
        CHECK (label_status IN (
            'pending', 'labeled', 'insufficient', 'invalid'
        )),

    CONSTRAINT transfer_samples_label_check
        CHECK (
            (
                label_status = 'labeled'
                AND front_observed_at IS NOT NULL
                AND next_observed_at IS NOT NULL
                AND labeled_at IS NOT NULL
                AND time_feasible IS NOT NULL
            )
            OR (
                label_status <> 'labeled'
                AND time_feasible IS NULL
            )
        ),

    CONSTRAINT transfer_samples_time_check
        CHECK (
            walk_seconds < 'Infinity'::double precision
            AND walk_seconds >= 0
            AND buffer_seconds < 'Infinity'::double precision
            AND buffer_seconds >= 0
        ),

    CONSTRAINT transfer_samples_probability_check
        CHECK (
            predicted_probability IS NULL
            OR predicted_probability BETWEEN 0 AND 1
        ),

    CONSTRAINT transfer_samples_json_check
        CHECK (
            jsonb_typeof(prediction_context) = 'object'
            AND jsonb_typeof(observation_context) = 'object'
            AND jsonb_typeof(probability_context) = 'object'
        )
);

CREATE INDEX transfer_samples_status_deadline_idx
    ON app.transfer_samples (label_status, label_deadline_at);

CREATE INDEX transfer_samples_stops_idx
    ON app.transfer_samples (
        front_stop_id, next_stop_id, evaluated_at
    );

CREATE INDEX transfer_samples_cohort_idx
    ON app.transfer_samples (cohort_key, label_status);

CREATE INDEX transfer_samples_independence_idx
    ON app.transfer_samples (independence_group);

COMMIT;
