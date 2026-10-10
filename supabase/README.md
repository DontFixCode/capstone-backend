# Database (Supabase PostgreSQL) — 1차 MVP

설계 문서(Database Structure 보완안, 2026-10-03)의 **1차 MVP 핵심 13개 테이블**을 구현한 마이그레이션입니다.
2차 MVP 11개 테이블(staff_time_off, 평판, 채팅, 신고, 알림)은 이후 별도 마이그레이션으로 추가합니다.

```
supabase/
├── migrations/20261010000000_mvp1_schema.sql   # 테이블·제약·트리거·RLS·권한
├── seed.sql                                     # 지역·업종·공통 서비스 종류
└── tests/
    ├── 00_local_supabase_stub.sql               # (로컬 PostgreSQL 테스트 전용) auth 흉내
    └── 01_mvp1_checks.sql                       # 41개 검증 시나리오
```

## DB가 직접 보장하는 것

| 영역 | 내용 |
| --- | --- |
| 중복 예약 | 같은 담당자의 `[start_at, end_at)` 점유 중첩을 GiST 배제 제약으로 차단 (동시 요청도 1건만 성공) |
| 스냅샷 | 가격·소요/정리시간·이름·직전 취소 기준은 DB 트리거가 현재 메뉴/업체 정책에서 계산 (클라이언트 값 무시) |
| 일정 검증 | 업체 행 잠금 후 영업시간·브레이크·근무시간·휴무·슬롯 단위·최소 사전 시간 검사 |
| 상태 전이 | 설계 문서 26장 표대로만 허용, 실행 주체(고객/업체/시스템) 확인, 직전 취소 자동 판정, `version` 자동 증가 |
| 일정 변경 보호 | 활성 예약과 충돌하는 휴무 등록·영업/근무시간 변경 거절 |
| 감사 로그 | `reservation_logs` 수정·삭제 불가, 스냅샷 필수 키·버전·사유·해시 형식 검사 |
| 권한 | anon/authenticated 직접 접근 차단, RLS 활성화, FastAPI용 `app_backend` / 작업용 `app_worker` 역할 |

## Supabase에 적용하기

### 방법 A — Supabase CLI (권장)
```bash
npm i -g supabase            # 또는 brew install supabase/tap/supabase
supabase login
supabase link --project-ref <프로젝트 ref>   # 대시보드 URL의 /project/<ref>
supabase db push             # migrations/ 적용
psql "<DB 연결 문자열>" -f supabase/seed.sql   # 시드 1회 (여러 번 실행해도 안전)
```

### 방법 B — 대시보드 SQL Editor
1. `migrations/20261010000000_mvp1_schema.sql` 전체를 붙여넣고 Run
2. `seed.sql` 전체를 붙여넣고 Run

> ⚠️ `tests/00_local_supabase_stub.sql`은 **실제 Supabase에 실행하지 마세요.** (auth 스키마는 Supabase가 관리)

### FastAPI 접속 계정 만들기 (1회, SQL Editor)
```sql
create role api_server login password '<강한 비밀번호>';
grant app_backend to api_server;
-- 만료 작업용 워커가 따로 있다면
create role api_worker login password '<강한 비밀번호>';
grant app_worker to api_worker;
```
`.env`에는 `DATABASE_URL=postgresql://api_server.<ref>:<비밀번호>@<pooler 호스트>:5432/postgres` 형태로 넣고 **절대 커밋하지 않습니다.**

## FastAPI에서 사용하는 방법

요청마다 JWT를 검증한 뒤 **같은 트랜잭션 안에서** 회원 문맥을 설정합니다.

```sql
begin;
select set_config('app.auth_user_id', '<검증된 JWT sub>', true);   -- true = 트랜잭션 한정
-- 예약 생성: 가격·상태 등은 보내지 않아도 DB가 채움
insert into reservations (user_id, shop_id, staff_id, service_id, start_at)
values (app.current_profile_id(), $1, $2, $3, $4) returning *;
-- 로그
insert into reservation_logs (reservation_id, action, after_snapshot, actor_type, actor_user_id, idempotency_key, request_hash)
values ($id, 'CREATE', app.reservation_snapshot(<위 행>), 'CUSTOMER', app.current_profile_id(), $idem_key, $sha256);
commit;
```

- 낙관적 잠금: `update reservations set ... where id = $1 and version = $2` → 0건이면 409
- 오류 코드 매핑: `23P01`(배제 제약) → **409 예약 불가**, `23514` → 400 잘못된 요청/전이, `42501` → 403
- 승인 만료: `app_worker`로 1분마다 `select app.expire_pending_reservations();` (pg_cron 또는 FastAPI 스케줄러)

## 로컬 테스트
```bash
createdb capstone_test
psql -d capstone_test -v ON_ERROR_STOP=1 \
  -f supabase/tests/00_local_supabase_stub.sql \
  -f supabase/migrations/20261010000000_mvp1_schema.sql \
  -f supabase/seed.sql \
  -f supabase/tests/01_mvp1_checks.sql
# 마지막에 ALL CHECKS PASSED
```

## 1차 MVP에서 아직 하지 않은 것
- 업체 등록 시 기본 담당자·근무시간·메뉴 연결 자동 생성 → FastAPI 3단계에서 처리
- 시간 범위 검색 쿼리 → FastAPI 5단계 (검색 결과는 조회 시점 기준, 저장 시 DB가 재검증)
- 2차 MVP 테이블 및 `staff_time_off` 충돌 검사
