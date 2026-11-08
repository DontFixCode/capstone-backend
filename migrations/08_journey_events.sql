\set ON_ERROR_STOP on

BEGIN;

CREATE TABLE app.journey_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    journey_id UUID NOT NULL
        REFERENCES app.journeys(id)
        ON DELETE CASCADE,

    idempotency_key TEXT NOT NULL,

    event_type TEXT NOT NULL,
    source TEXT NOT NULL,

    occurred_at TIMESTAMPTZ,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    state_version_after BIGINT NOT NULL,

    payload JSONB NOT NULL,

    CONSTRAINT journey_events_unique_request
        UNIQUE (journey_id, idempotency_key),

    CONSTRAINT journey_events_event_type_check
        CHECK (
            event_type IN (
                'board',
                'alight',
                'missed_bus',
                'destination_changed',
                'route_accepted',
                'route_rejected',
                'transfer_result',
                'evaluation_snapshot',
                'completed',
                'cancelled',
                'correction'
            )
        ),

    CONSTRAINT journey_events_source_check
        CHECK (source IN ('manual', 'system')),

    CONSTRAINT journey_events_version_check
        CHECK (state_version_after >= 1),

    CONSTRAINT journey_events_payload_check
        CHECK (jsonb_typeof(payload) = 'object')
);

CREATE INDEX journey_events_journey_received_idx
    ON app.journey_events (
        journey_id,
        received_at,
        id
    );

COMMIT;
