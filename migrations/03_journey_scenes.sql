\set ON_ERROR_STOP on

BEGIN;

CREATE TABLE app.journey_scenes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    scene_key TEXT NOT NULL UNIQUE,
    cohort_key TEXT NOT NULL,
    independence_group TEXT NOT NULL,

    information_at TIMESTAMPTZ NOT NULL,
    static_version UUID NOT NULL,

    route_context JSONB NOT NULL,
    predictions JSONB NOT NULL,
    observations JSONB NOT NULL DEFAULT '[]'::jsonb,
    recovery_context JSONB NOT NULL DEFAULT '{}'::jsonb,

    status TEXT NOT NULL DEFAULT 'pending',
    quality JSONB NOT NULL DEFAULT '{}'::jsonb,

    label_deadline_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT journey_scenes_status_check
        CHECK (status IN (
            'pending', 'complete', 'insufficient', 'invalid'
        )),

    CONSTRAINT journey_scenes_completed_check
        CHECK (status <> 'complete' OR completed_at IS NOT NULL),

    CONSTRAINT journey_scenes_json_check
        CHECK (
            jsonb_typeof(route_context) = 'object'
            AND jsonb_typeof(predictions) = 'array'
            AND jsonb_typeof(observations) = 'array'
            AND jsonb_typeof(recovery_context) = 'object'
            AND jsonb_typeof(quality) = 'object'
        )
);

CREATE INDEX journey_scenes_cohort_idx
    ON app.journey_scenes (cohort_key, information_at);

CREATE INDEX journey_scenes_status_deadline_idx
    ON app.journey_scenes (status, label_deadline_at);

CREATE INDEX journey_scenes_independence_idx
    ON app.journey_scenes (independence_group);

COMMIT;
