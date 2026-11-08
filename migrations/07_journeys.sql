\set ON_ERROR_STOP on

BEGIN;

CREATE TABLE app.journeys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    user_id UUID NOT NULL
        REFERENCES app.profiles(user_id)
        ON DELETE CASCADE,

    initial_search_id UUID
        REFERENCES app.route_searches(id)
        ON DELETE SET NULL,

    selected_candidate_id UUID
        REFERENCES app.route_candidates(id)
        ON DELETE SET NULL,

    proposed_candidate_id UUID
        REFERENCES app.route_candidates(id)
        ON DELETE SET NULL,

    selected_snapshot JSONB NOT NULL,
    proposed_snapshot JSONB,

    lifecycle_status TEXT NOT NULL,
    movement_state TEXT NOT NULL,
    connection_state TEXT NOT NULL,

    current_progress JSONB NOT NULL DEFAULT '{}'::jsonb,

    current_location extensions.geography(Point,4326),

    location_observed_at TIMESTAMPTZ,
    location_received_at TIMESTAMPTZ,
    location_accuracy_m DOUBLE PRECISION,

    last_connected_at TIMESTAMPTZ,

    state_version BIGINT NOT NULL DEFAULT 1,
    selection_version BIGINT NOT NULL DEFAULT 1,

    next_recalc_at TIMESTAMPTZ,

    result_version BIGINT NOT NULL DEFAULT 0,

    guidance JSONB NOT NULL DEFAULT '{}'::jsonb,
    notify_state JSONB NOT NULL DEFAULT '{}'::jsonb,

    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,

    CONSTRAINT journeys_lifecycle_check
        CHECK (
            lifecycle_status IN (
                'active', 'completed', 'cancelled'
            )
        ),

    CONSTRAINT journeys_movement_check
        CHECK (
            movement_state IN (
                'walking',
                'waiting',
                'onboard',
                'transferring',
                'arrived'
            )
        ),

    CONSTRAINT journeys_connection_check
        CHECK (
            connection_state IN (
                'connected',
                'disconnected',
                'unknown'
            )
        ),

    CONSTRAINT journeys_ended_check
        CHECK (
            (lifecycle_status = 'active'
                AND ended_at IS NULL)
            OR
            (lifecycle_status IN ('completed', 'cancelled')
                AND ended_at IS NOT NULL)
        ),

    CONSTRAINT journeys_recalc_check
        CHECK (
            (lifecycle_status = 'active'
                AND next_recalc_at IS NOT NULL)
            OR
            (lifecycle_status <> 'active'
                AND next_recalc_at IS NULL)
        ),

    CONSTRAINT journeys_version_check
        CHECK (
            state_version >= 1
            AND selection_version >= 1
            AND result_version >= 0
        ),

    CONSTRAINT journeys_location_accuracy_check
        CHECK (
            location_accuracy_m IS NULL
            OR (
                location_accuracy_m >= 0
                AND location_accuracy_m < 'Infinity'::double precision
            )
        ),

    CONSTRAINT journeys_json_check
        CHECK (
            jsonb_typeof(selected_snapshot) = 'object'
            AND (
                proposed_snapshot IS NULL
                OR jsonb_typeof(proposed_snapshot) = 'object'
            )
            AND jsonb_typeof(current_progress) = 'object'
            AND jsonb_typeof(guidance) = 'object'
            AND jsonb_typeof(notify_state) = 'object'
        )
);

CREATE INDEX journeys_user_started_idx
    ON app.journeys (user_id, started_at DESC);

CREATE INDEX journeys_next_recalc_idx
    ON app.journeys (next_recalc_at)
    WHERE lifecycle_status = 'active';

CREATE UNIQUE INDEX journeys_one_active_per_user_idx
    ON app.journeys (user_id)
    WHERE lifecycle_status = 'active';

COMMIT;
