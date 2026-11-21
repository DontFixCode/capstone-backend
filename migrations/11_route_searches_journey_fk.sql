\set ON_ERROR_STOP on

-- route_searches.journey_id → journeys(id) ON DELETE CASCADE (Notion Database Structure 7. route_searches 제약 조건).
-- 05_route_searches.sql은 journeys보다 먼저 만들어 이 FK가 없었다(설계 적용 순서의 "journeys의 순환 FK 추가" 단계).
-- 이동 기록 삭제(DELETE /api/v1/journeys/{id})는 재평가 검색을 명시적으로 먼저 지우므로 이 FK가 없어도 동작하지만,
-- 소속 이동이 없는 재평가 검색이 남지 않도록 DB에서도 보장한다.
-- 같은 컬럼에 journeys를 참조하는 FK가 이미 있으면(이름과 관계없이) 아무것도 바꾸지 않는다. 재실행해도 결과가 같다.
-- 추가 전에 소속 이동이 없는 journey_id가 있으면 FK 검증이 실패하므로, 그런 행을 찾아 NOTICE로 알리고 중단한다(행을 지우지 않음).

BEGIN;

DO $$
DECLARE
    dangling BIGINT;
BEGIN
    IF EXISTS (
        SELECT 1
          FROM pg_constraint c
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
         WHERE c.conrelid = 'app.route_searches'::regclass
           AND c.contype = 'f'
           AND c.confrelid = 'app.journeys'::regclass
           AND a.attname = 'journey_id'
    ) THEN
        RAISE NOTICE 'route_searches.journey_id FK가 이미 있어 건너뜀';
        RETURN;
    END IF;

    SELECT count(*) INTO dangling
      FROM app.route_searches s
     WHERE s.journey_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM app.journeys j WHERE j.id = s.journey_id);
    IF dangling > 0 THEN
        RAISE EXCEPTION '소속 이동이 없는 재평가 검색 %건이 있어 FK를 추가하지 않음(데이터를 확인한 뒤 다시 실행)', dangling;
    END IF;

    ALTER TABLE app.route_searches
        ADD CONSTRAINT route_searches_journey_id_fkey
        FOREIGN KEY (journey_id) REFERENCES app.journeys(id) ON DELETE CASCADE;
END
$$;

COMMIT;
