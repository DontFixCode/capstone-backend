\set ON_ERROR_STOP on

-- 화면 조회 기반 실시간 수집 대상(Notion Development Phase > Phase 4A 후속 보완, 2026-11-08).
-- GET /routes/{id}/positions·/stops/{id}/arrivals가 등록하고 단일 수집 워커가 읽는다. 기존 수집 대상 키
-- (app.api_fetches.target_key와 같은 제공처·수집 종류·조회 조건 키)와 만료 시각만 둔다. 재실행해도 결과가 같다.

BEGIN;

CREATE TABLE IF NOT EXISTS app.realtime_view_targets (
    target_key TEXT PRIMARY KEY,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS realtime_view_targets_expires_idx
    ON app.realtime_view_targets (expires_at);

-- 실행 주체별 최소 권한. 마이그레이션 실행 시점에 있는 역할에만 부여하므로, 배포 전에 실제 접속 역할 생성과
-- app 스키마 USAGE 권한을 확인한다(없으면 NOTICE로 알린다).
--   app_api          API 서버: 대상 등록·TTL 연장(INSERT ... ON CONFLICT DO UPDATE ... RETURNING)
--   app_worker       수집 워커: 유효 대상 조회, 만료 행 정리
--   bus_transfer_api 현재 대상 DB에서 API·워커가 함께 쓰는 단일 접속 역할
DO $$
DECLARE
    grant_row RECORD;
BEGIN
    FOR grant_row IN
        SELECT * FROM (VALUES
            ('app_api', 'SELECT, INSERT, UPDATE'),
            ('app_worker', 'SELECT, DELETE'),
            ('bus_transfer_api', 'SELECT, INSERT, UPDATE, DELETE')
        ) AS g(role_name, privileges)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = grant_row.role_name) THEN
            RAISE NOTICE '역할 %이 없어 app.realtime_view_targets 권한을 건너뜀', grant_row.role_name;
            CONTINUE;
        END IF;
        EXECUTE format('GRANT %s ON app.realtime_view_targets TO %I', grant_row.privileges, grant_row.role_name);
        IF NOT has_schema_privilege(grant_row.role_name, 'app', 'USAGE') THEN
            RAISE NOTICE '역할 %에 app 스키마 USAGE 권한이 없어 테이블 권한만으로는 접근할 수 없음', grant_row.role_name;
        END IF;
    END LOOP;
END
$$;

COMMIT;
