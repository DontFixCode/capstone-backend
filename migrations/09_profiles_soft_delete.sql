\set ON_ERROR_STOP on

-- profiles 소프트 삭제 컬럼(Notion Database Structure 1. profiles, 2026-11-07 기준).
-- 02_profiles.sql로 새로 만든 DB에는 이미 있으므로 아무것도 바꾸지 않고, 컬럼 추가 전의 DB에는 컬럼과 제약을 더한다.
-- 재실행해도 결과가 같다.

BEGIN;

ALTER TABLE app.profiles
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS auth_blocked_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'app.profiles'::regclass AND conname = 'profiles_auth_blocked_check'
    ) THEN
        ALTER TABLE app.profiles
            ADD CONSTRAINT profiles_auth_blocked_check
            CHECK (
                auth_blocked_at IS NULL
                OR (deleted_at IS NOT NULL AND auth_blocked_at >= deleted_at)
            );
    END IF;
END
$$;

COMMIT;
