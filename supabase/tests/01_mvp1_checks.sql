-- 1차 MVP 스키마 검증 스크립트 (로컬 테스트 DB 전용)
--   psql -v ON_ERROR_STOP=1 -f 00_local_supabase_stub.sql -f ../migrations/*.sql -f ../seed.sql -f 01_mvp1_checks.sql
-- 실패 시 예외로 중단됩니다. 마지막에 "ALL CHECKS PASSED"가 출력되면 성공.

create or replace function pg_temp.expect_error(p_sql text, p_state text, p_label text)
returns void language plpgsql as $$
begin
  execute p_sql;
  raise exception 'FAIL [%]: 오류가 나야 하는데 성공함', p_label;
exception when others then
  if sqlstate like 'P0001' and sqlerrm like 'FAIL [%' then raise; end if;
  if p_state is not null and sqlstate <> p_state then
    raise exception 'FAIL [%]: 기대 %, 실제 % (%)', p_label, p_state, sqlstate, sqlerrm;
  end if;
  raise notice 'ok  - % (%: %)', p_label, sqlstate, sqlerrm;
end $$;

create or replace function pg_temp.ok(p_cond boolean, p_label text)
returns void language plpgsql as $$
begin
  if not coalesce(p_cond, false) then raise exception 'FAIL [%]', p_label; end if;
  raise notice 'ok  - %', p_label;
end $$;

do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'test_backend') then
    create role test_backend login;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'test_worker') then
    create role test_worker login;
  end if;
end $$;
grant app_backend to test_backend;
grant app_worker to test_worker;

-- ---------------------------------------------------------------
-- 1. Auth 가입 → 프로필 자동 생성 (ADMIN 메타데이터 무시)
-- ---------------------------------------------------------------
insert into auth.users (id, email, raw_user_meta_data) values
  ('00000000-0000-0000-0000-00000000000a', 'owner@test.com',  '{"name":"사장님","platform_role":"ADMIN"}'),
  ('00000000-0000-0000-0000-00000000000b', 'alice@test.com',  '{"name":"앨리스"}'),
  ('00000000-0000-0000-0000-00000000000c', 'bob@test.com',    '{}');

select pg_temp.ok((select count(*) = 3 from profiles where platform_role = 'USER'), '가입 시 프로필 생성, 권한은 USER 고정');
select pg_temp.ok((select name = 'bob' from profiles where auth_user_id = '00000000-0000-0000-0000-00000000000c'), '이름 없으면 이메일 앞부분 사용');
select pg_temp.expect_error($q$ delete from auth.users where id = '00000000-0000-0000-0000-00000000000b' $q$,
  '23514', '활성 회원 Auth 삭제는 차단 (탈퇴 절차 선행 필요)');

-- 클라이언트 롤은 테이블 접근 불가
set role authenticated;
select pg_temp.expect_error('select * from public.profiles', '42501', 'authenticated 직접 접근 차단');
reset role;
set role anon;
select pg_temp.expect_error('select * from public.reservations', '42501', 'anon 직접 접근 차단');
reset role;

-- ---------------------------------------------------------------
-- 2. 업체 관리자: 업체·영업시간·담당자·메뉴 등록
-- ---------------------------------------------------------------
set role test_backend;
select set_config('app.auth_user_id', '00000000-0000-0000-0000-00000000000a', false);

select pg_temp.expect_error($q$ update profiles set platform_role = 'ADMIN' $q$, '42501', '본인 역할 변경 차단');
update profiles set phone = '010-1111-2222' where auth_user_id = '00000000-0000-0000-0000-00000000000a';

insert into shops (id, owner_id, category_id, region_id, name, address, late_cancel_hours)
select '10000000-0000-0000-0000-000000000001', app.current_profile_id(),
       (select id from categories where code = 'HAIR'),
       (select id from regions where sigungu = '공주시'), '공주 헤어', '충남 공주시 어딘가 1', 3;

-- 다른 사람 명의 업체 등록 차단
select pg_temp.expect_error($q$
  insert into shops (owner_id, category_id, region_id, name, address)
  select (select id from profiles where name = '앨리스'), category_id, region_id, 'x', 'y' from shops limit 1 $q$,
  null, '타인 명의 업체 등록 차단');

-- 매일 10:00~13:00, 14:00~20:00 (13~14 브레이크)
insert into shop_business_hours (shop_id, day_of_week, open_time, close_time)
select '10000000-0000-0000-0000-000000000001', d, t.o, t.c
from generate_series(0,6) d, (values (time '10:00', time '13:00'), (time '14:00', time '20:00')) t(o,c);

select pg_temp.expect_error($q$ insert into shop_business_hours (shop_id, day_of_week, open_time, close_time)
  values ('10000000-0000-0000-0000-000000000001', 1, '12:00', '15:00') $q$, '23P01', '영업 구간 중첩 차단');

insert into staff (id, shop_id, name) values
  ('20000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', '김디자이너'),
  ('20000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001', '이디자이너');
insert into staff_work_hours (staff_id, day_of_week, start_time, end_time)
select s, h.day_of_week, h.open_time, h.close_time
from shop_business_hours h,
     unnest(array['20000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000002']::uuid[]) s
where h.shop_id = '10000000-0000-0000-0000-000000000001';

insert into services (id, shop_id, category_id, service_type_id, name, price, duration_min, buffer_min)
select '30000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001',
       c.id, st.id, '남성 커트', 20000, 30, 10
from categories c join service_types st on st.category_id = c.id
where c.code = 'HAIR' and st.code = 'HAIR_CUT';

-- 업종 불일치 메뉴 차단 (미용실에 네일 서비스 종류)
select pg_temp.expect_error($q$
  insert into services (shop_id, category_id, service_type_id, name, price, duration_min)
  select '10000000-0000-0000-0000-000000000001', c.id, st.id, '젤', 1, 10
  from categories c join service_types st on st.category_id = c.id where c.code = 'NAIL' limit 1 $q$,
  '23503', '업체 업종과 다른 서비스 종류 차단');

insert into staff_services (shop_id, staff_id, service_id) values
  ('10000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001'),
  ('10000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000002', '30000000-0000-0000-0000-000000000001');

-- ---------------------------------------------------------------
-- 3. 고객 예약 (2026-12-01 = 화요일, KST)
-- ---------------------------------------------------------------
select set_config('app.auth_user_id', '00000000-0000-0000-0000-00000000000b', false);

insert into reservations (id, user_id, shop_id, staff_id, service_id, start_at,
  service_end_at, end_at, duration_min, late_cancel_hours, status, shop_name, staff_name, service_name, price)
values ('40000000-0000-0000-0000-000000000001', app.current_profile_id(),
  '10000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001',
  '2026-12-01 14:00+09', now(), now(), 1, 0, 'CONFIRMED', '조작', '조작', '조작', 1);

select pg_temp.ok((select price = 20000 and status = 'PENDING' and shop_name = '공주 헤어'
                     and end_at = '2026-12-01 14:40+09' and service_end_at = '2026-12-01 14:30+09'
                     and approval_expires_at is not null and version = 1
                   from reservations where id = '40000000-0000-0000-0000-000000000001'),
  '클라이언트 값 무시하고 DB가 스냅샷·상태·만료시각 계산');

insert into reservation_logs (reservation_id, action, after_snapshot, actor_type, actor_user_id, idempotency_key, request_hash)
select r.id, 'CREATE', app.reservation_snapshot(r), 'CUSTOMER', app.current_profile_id(),
       '50000000-0000-0000-0000-000000000001', repeat('a', 64)
from reservations r where r.id = '40000000-0000-0000-0000-000000000001';

select pg_temp.expect_error($q$ insert into reservations (user_id, shop_id, staff_id, service_id, start_at)
  values ((select id from profiles where name='사장님'), '10000000-0000-0000-0000-000000000001',
  '20000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001', '2026-12-01 16:00+09') $q$,
  null, '타인 명의 예약 차단');

-- bob 으로 전환
select set_config('app.auth_user_id', '00000000-0000-0000-0000-00000000000c', false);
select pg_temp.ok((select count(*) = 0 from reservations), '다른 고객의 예약은 보이지 않음');

select pg_temp.expect_error($q$ insert into reservations (user_id, shop_id, staff_id, service_id, start_at)
  values (app.current_profile_id(), '10000000-0000-0000-0000-000000000001',
  '20000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001', '2026-12-01 14:30+09') $q$,
  '23P01', '같은 담당자 정리시간(14:30~14:40) 겹침 차단');

insert into reservations (user_id, shop_id, staff_id, service_id, start_at)
values (app.current_profile_id(), '10000000-0000-0000-0000-000000000001',
  '20000000-0000-0000-0000-000000000002', '30000000-0000-0000-0000-000000000001', '2026-12-01 14:00+09');
select pg_temp.ok(true, '다른 담당자는 같은 시간 예약 가능');

-- 14:45는 슬롯 단위(15분) OK, 앞 예약 end_at 14:40 이후
insert into reservations (id, user_id, shop_id, staff_id, service_id, start_at)
values ('40000000-0000-0000-0000-000000000002', app.current_profile_id(), '10000000-0000-0000-0000-000000000001',
  '20000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001', '2026-12-01 14:45+09');
select pg_temp.ok(true, '앞 예약 점유 종료 이후 예약 가능');

select pg_temp.expect_error($q$ insert into reservations (user_id, shop_id, staff_id, service_id, start_at)
  values (app.current_profile_id(), '10000000-0000-0000-0000-000000000001',
  '20000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001', '2026-12-01 12:30+09') $q$,
  '23514', '브레이크타임 걸치는 예약 차단 (12:30~13:10)');
select pg_temp.expect_error($q$ insert into reservations (user_id, shop_id, staff_id, service_id, start_at)
  values (app.current_profile_id(), '10000000-0000-0000-0000-000000000001',
  '20000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001', '2026-12-01 15:05+09') $q$,
  '23514', '슬롯 단위에 맞지 않는 시각 차단');
select pg_temp.expect_error($q$ insert into reservations (user_id, shop_id, staff_id, service_id, start_at)
  values (app.current_profile_id(), '10000000-0000-0000-0000-000000000001',
  '20000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001', '2020-12-01 14:00+09') $q$,
  '23514', '과거 시각 예약 차단');

-- 고객은 승인 불가
select pg_temp.expect_error($q$ update reservations set status = 'CONFIRMED'
  where id = '40000000-0000-0000-0000-000000000002' $q$, '42501', '고객 승인 시도 차단');

-- ---------------------------------------------------------------
-- 4. 업체 처리 · 휴무 · 영업시간 충돌
-- ---------------------------------------------------------------
select set_config('app.auth_user_id', '00000000-0000-0000-0000-00000000000a', false);
select pg_temp.ok((select count(*) = 3 from reservations), '업체는 본인 업체 예약 전체 조회');

update reservations set status = 'CONFIRMED' where id = '40000000-0000-0000-0000-000000000001';
select pg_temp.ok((select status = 'CONFIRMED' and version = 2 from reservations
                   where id = '40000000-0000-0000-0000-000000000001'), '업체 승인 + 버전 증가');

select pg_temp.expect_error($q$ update reservations set price = 0
  where id = '40000000-0000-0000-0000-000000000001' $q$, '42501', '가격 스냅샷 직접 수정 차단');
select pg_temp.expect_error($q$ update reservations set status = 'COMPLETED'
  where id = '40000000-0000-0000-0000-000000000001' $q$, '23514', '서비스 종료 전 완료 처리 차단');

select pg_temp.expect_error($q$ insert into shop_closures (shop_id, closed_date)
  values ('10000000-0000-0000-0000-000000000001', '2026-12-01') $q$, '23P01', '예약 있는 날 휴무 등록 차단');
insert into shop_closures (shop_id, closed_date, reason) values ('10000000-0000-0000-0000-000000000001', '2026-12-02', '정기 점검');

select pg_temp.expect_error($q$ update shop_business_hours set open_time = '15:00'
  where shop_id = '10000000-0000-0000-0000-000000000001' and day_of_week = 2 and open_time = '14:00' $q$,
  '23P01', '기존 예약과 충돌하는 영업시간 변경 차단');

update reservations set status = 'REJECTED', reject_reason = '해당 시간 예약 마감'
  where id = '40000000-0000-0000-0000-000000000002';
select pg_temp.expect_error($q$ update reservations set status = 'CONFIRMED'
  where id = '40000000-0000-0000-0000-000000000002' $q$, '23514', '종료 상태(REJECTED) 변경 차단');

-- 고객 예약 (휴무일 차단)
select set_config('app.auth_user_id', '00000000-0000-0000-0000-00000000000c', false);
select pg_temp.expect_error($q$ insert into reservations (user_id, shop_id, staff_id, service_id, start_at)
  values (app.current_profile_id(), '10000000-0000-0000-0000-000000000001',
  '20000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001', '2026-12-02 14:00+09') $q$,
  '23514', '휴무일 예약 차단');

-- ---------------------------------------------------------------
-- 5. 고객 일정 변경 · 취소 (일반/직전)
-- ---------------------------------------------------------------
select set_config('app.auth_user_id', '00000000-0000-0000-0000-00000000000b', false);
update reservations set start_at = '2026-12-01 16:00+09' where id = '40000000-0000-0000-0000-000000000001';
select pg_temp.ok((select status = 'PENDING' and version = 3 and end_at = '2026-12-01 16:40+09'
                   from reservations where id = '40000000-0000-0000-0000-000000000001'),
  '일정 변경 → 수동 승인 업체는 PENDING 재승인, 버전 증가');


update reservations set status = 'CANCELED', canceled_by = 'USER', cancel_reason = '일정 변경'
  where id = '40000000-0000-0000-0000-000000000001';
select pg_temp.ok((select status = 'CANCELED' and not is_late_cancel and canceled_at is not null
                   from reservations where id = '40000000-0000-0000-0000-000000000001'), '일반 취소');

select pg_temp.expect_error($q$ insert into reservation_logs (reservation_id, action, after_snapshot, actor_type,
  actor_user_id, idempotency_key, request_hash)
  select id, 'CANCEL', app.reservation_snapshot(r), 'CUSTOMER', app.current_profile_id(), gen_random_uuid(), repeat('b',64)
  from reservations r where id = '40000000-0000-0000-0000-000000000001' $q$, '23514', '로그 사유·이전 스냅샷 누락 차단');

-- 직전 취소: 업체 기준을 72시간으로 바꾼 뒤 내일 예약
select set_config('app.auth_user_id', '00000000-0000-0000-0000-00000000000a', false);
update shops set late_cancel_hours = 72 where id = '10000000-0000-0000-0000-000000000001';
select set_config('app.auth_user_id', '00000000-0000-0000-0000-00000000000b', false);
insert into reservations (id, user_id, shop_id, staff_id, service_id, start_at)
values ('40000000-0000-0000-0000-000000000003', app.current_profile_id(), '10000000-0000-0000-0000-000000000001',
  '20000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001',
  ((now() at time zone 'Asia/Seoul')::date + 1 + time '10:00') at time zone 'Asia/Seoul');
update reservations set status = 'CANCELED', canceled_by = 'USER', cancel_reason = '급한 일'
  where id = '40000000-0000-0000-0000-000000000003';
select pg_temp.ok((select is_late_cancel and late_cancel_hours = 72 from reservations
                   where id = '40000000-0000-0000-0000-000000000003'), '직전 취소 판정 + 정책 스냅샷');

insert into reservations (id, user_id, shop_id, staff_id, service_id, start_at)
values ('40000000-0000-0000-0000-000000000004', app.current_profile_id(), '10000000-0000-0000-0000-000000000001',
  '20000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001', '2026-12-01 17:00+09');
select pg_temp.expect_error($q$ update reservations set status = 'CANCELED', canceled_by = 'SHOP', cancel_reason = 'x'
  where id = '40000000-0000-0000-0000-000000000004' $q$, '42501', '고객이 업체 취소 주체 사칭 차단');

-- 로그 불변
reset role;
select pg_temp.expect_error($q$ update reservation_logs set reason = 'x' $q$, '42501', '로그 수정 차단');
select pg_temp.expect_error($q$ delete from reservation_logs $q$, '42501', '로그 삭제 차단');

-- ---------------------------------------------------------------
-- 6. 시간 경과가 필요한 전이: 트리거를 우회해 과거 데이터를 만든 뒤 검증
-- ---------------------------------------------------------------
set session_replication_role = replica;
insert into reservations (id, user_id, shop_id, staff_id, service_id, start_at, service_end_at, end_at,
  duration_min, buffer_min, late_cancel_hours, status, requested_at, approval_expires_at,
  shop_name, staff_name, service_name, price, created_at)
select v.id, p.id, '10000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000002',
  '30000000-0000-0000-0000-000000000001', v.s, v.s + interval '30 min', v.s + interval '40 min',
  30, 10, 3, v.st, v.s - interval '2 day', v.exp, '공주 헤어', '이디자이너', '남성 커트', 20000, v.s - interval '2 day'
from profiles p, (values
  ('40000000-0000-0000-0000-000000000010'::uuid, now() - interval '2 hour', 'CONFIRMED', null::timestamptz),
  ('40000000-0000-0000-0000-000000000011'::uuid, now() + interval '5 hour', 'PENDING',  now() - interval '1 min')
) v(id, s, st, exp)
where p.name = '앨리스';
set session_replication_role = origin;

set role test_backend;
select set_config('app.auth_user_id', '00000000-0000-0000-0000-00000000000a', false);
update reservations set status = 'COMPLETED' where id = '40000000-0000-0000-0000-000000000010';
select pg_temp.ok(true, '서비스 종료 후 이용 완료 처리');
select pg_temp.expect_error($q$ update reservations set status = 'CONFIRMED'
  where id = '40000000-0000-0000-0000-000000000011' $q$, '23514', '승인 마감 지난 늦은 승인 차단');
select pg_temp.expect_error($q$ select app.expire_pending_reservations() $q$, '42501', 'app_backend는 만료 작업 실행 불가');

reset role;
set role test_worker;
select pg_temp.ok((select app.expire_pending_reservations() = 1), '만료 작업이 PENDING → EXPIRED 처리');
select pg_temp.ok((select app.expire_pending_reservations() = 0), '만료 작업 재실행해도 중복 없음');
select pg_temp.ok((select count(*) = 1 from reservation_logs where action = 'EXPIRE'), '만료 로그 1건 기록');
reset role;

select 'ALL CHECKS PASSED' as result;
