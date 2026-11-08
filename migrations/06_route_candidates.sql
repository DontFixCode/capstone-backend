\set ON_ERROR_STOP on

BEGIN;

CREATE TABLE app.route_candidates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    search_id UUID NOT NULL
        REFERENCES app.route_searches(id) ON DELETE CASCADE,

    static_route_key TEXT NOT NULL,
    option_key TEXT NOT NULL,

    legs JSONB NOT NULL,
    boarding_count INTEGER NOT NULL,

    walking_seconds DOUBLE PRECISION,
    nominal_total_seconds DOUBLE PRECISION,
    expected_total_seconds DOUBLE PRECISION,
    journey_probability NUMERIC(8,6),

    time_status TEXT NOT NULL,
    nominally_feasible BOOLEAN,

    evaluation JSONB NOT NULL DEFAULT '{}'::jsonb,

    ranking_mode TEXT,
    rank_no INTEGER,

    evaluated_at TIMESTAMPTZ NOT NULL,

    CONSTRAINT route_candidates_unique_option
        UNIQUE (search_id, static_route_key, option_key),

    CONSTRAINT route_candidates_search_ranking_fkey
        FOREIGN KEY (search_id, ranking_mode)
        REFERENCES app.route_searches(id, ranking_mode)
        ON UPDATE CASCADE ON DELETE CASCADE,

    CONSTRAINT route_candidates_boarding_check
        CHECK (boarding_count >= 1),

    CONSTRAINT route_candidates_time_status_check
        CHECK (time_status IN ('available', 'partial', 'unavailable')),

    CONSTRAINT route_candidates_ranking_mode_check
        CHECK (ranking_mode IS NULL OR ranking_mode IN ('nominal', 'expected')),

    CONSTRAINT route_candidates_rank_pair_check
        CHECK (
            (rank_no IS NULL AND ranking_mode IS NULL)
            OR
            (rank_no IS NOT NULL AND rank_no >= 1 AND ranking_mode IS NOT NULL)
        ),

    CONSTRAINT route_candidates_nominal_rank_check
        CHECK (
            ranking_mode IS DISTINCT FROM 'nominal'
            OR (
                nominally_feasible IS TRUE
                AND time_status = 'available'
                AND nominal_total_seconds IS NOT NULL
            )
        ),

    CONSTRAINT route_candidates_expected_rank_check
        CHECK (
            ranking_mode IS DISTINCT FROM 'expected'
            OR expected_total_seconds IS NOT NULL
        ),

    CONSTRAINT route_candidates_nonnegative_times_check
        CHECK (
            (walking_seconds IS NULL OR
                (walking_seconds >= 0 AND walking_seconds < 'Infinity'::double precision))
            AND
            (nominal_total_seconds IS NULL OR
                (nominal_total_seconds >= 0 AND nominal_total_seconds < 'Infinity'::double precision))
            AND
            (expected_total_seconds IS NULL OR
                (expected_total_seconds >= 0 AND expected_total_seconds < 'Infinity'::double precision))
        ),

    CONSTRAINT route_candidates_probability_check
        CHECK (
            journey_probability IS NULL
            OR journey_probability BETWEEN 0 AND 1
        ),

    CONSTRAINT route_candidates_json_check
        CHECK (
            jsonb_typeof(legs) = 'array'
            AND jsonb_typeof(evaluation) = 'object'
        )
);

CREATE UNIQUE INDEX route_candidates_search_rank_unique
    ON app.route_candidates (search_id, rank_no)
    WHERE rank_no IS NOT NULL;

COMMIT;
