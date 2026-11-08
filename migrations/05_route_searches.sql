\set ON_ERROR_STOP on

BEGIN;

CREATE TABLE app.route_searches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    user_id UUID
        REFERENCES app.profiles(user_id) ON DELETE CASCADE,

    journey_id UUID,

    source TEXT NOT NULL,

    origin extensions.geography(Point,4326) NOT NULL,
    destination extensions.geography(Point,4326) NOT NULL,

    place_info JSONB NOT NULL DEFAULT '{}'::jsonb,

    start_at TIMESTAMPTZ NOT NULL,
    static_version UUID NOT NULL,
    config_version TEXT NOT NULL,

    params JSONB NOT NULL,
    ranking_mode TEXT NOT NULL,
    status TEXT NOT NULL,

    summary JSONB NOT NULL DEFAULT '{}'::jsonb,

    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT route_searches_source_check
        CHECK (source IN ('app', 'cli', 'worker')),

    CONSTRAINT route_searches_ranking_mode_check
        CHECK (ranking_mode IN ('nominal', 'expected')),

    CONSTRAINT route_searches_status_check
        CHECK (status IN (
            'processing',
            'ready',
            'out_of_scope',
            'no_path',
            'data_limited',
            'error'
        )),

    CONSTRAINT route_searches_user_check
        CHECK (
            (source <> 'app' OR user_id IS NOT NULL)
            AND
            (
                source <> 'worker'
                OR (user_id IS NOT NULL AND journey_id IS NOT NULL)
            )
            AND
            (journey_id IS NULL OR user_id IS NOT NULL)
        ),

    CONSTRAINT route_searches_json_check
        CHECK (
            jsonb_typeof(place_info) = 'object'
            AND jsonb_typeof(params) = 'object'
            AND jsonb_typeof(summary) = 'object'
        ),

    CONSTRAINT route_searches_id_mode_unique
        UNIQUE (id, ranking_mode)
);

CREATE INDEX route_searches_user_created_idx
    ON app.route_searches (user_id, created_at DESC);

CREATE INDEX route_searches_status_expires_idx
    ON app.route_searches (status, expires_at);

CREATE INDEX route_searches_journey_created_idx
    ON app.route_searches (journey_id, created_at DESC)
    WHERE journey_id IS NOT NULL;

COMMIT;
