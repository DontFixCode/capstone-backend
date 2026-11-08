\set ON_ERROR_STOP on

BEGIN;

CREATE TABLE app.profiles (
    user_id UUID PRIMARY KEY
        REFERENCES auth.users(id) ON DELETE CASCADE,
    display_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    auth_blocked_at TIMESTAMPTZ,

    CONSTRAINT profiles_auth_blocked_check
        CHECK (
            auth_blocked_at IS NULL
            OR (deleted_at IS NOT NULL AND auth_blocked_at >= deleted_at)
        )
);

COMMIT;
