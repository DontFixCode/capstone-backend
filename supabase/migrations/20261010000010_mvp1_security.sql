-- 1차 MVP 스키마 (10/10): 권한(GRANT) · RLS

-- =====================================================================
-- 13. 권한 (GRANT) · RLS
--   - 클라이언트(anon/authenticated)의 Data API 직접 접근 차단
--   - app_backend: RLS 적용, 요청 회원 문맥으로 제한
--   - app_worker : 서버 내부 작업, 전체 허용 정책
-- =====================================================================
do $$
declare t text;
begin
  foreach t in array array['profiles','regions','categories','service_types','shops',
    'shop_business_hours','shop_closures','services','staff','staff_services',
    'staff_work_hours','reservations','reservation_logs']
  loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on public.%I from public', t);
    if exists (select 1 from pg_roles where rolname = 'anon') then
      execute format('revoke all on public.%I from anon', t);
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
      execute format('revoke all on public.%I from authenticated', t);
    end if;
    execute format('grant select, insert, update on public.%I to app_worker', t);
    execute format('create policy worker_all on public.%I for all to app_worker using (true) with check (true)', t);
  end loop;
end $$;

revoke all on all functions in schema app from public;
grant execute on all functions in schema app to app_backend, app_worker;
revoke execute on function app.expire_pending_reservations(integer) from app_backend;
revoke execute on function app.handle_new_auth_user() from app_backend, app_worker;

-- profiles: 본인 조회, 이름·연락처만 수정. 역할·상태 변경은 app_worker 경로
grant select on public.profiles to app_backend;
grant update (name, phone) on public.profiles to app_backend;
create policy profiles_select on public.profiles for select to app_backend
  using (id = app.current_profile_id() or app.is_admin());
create policy profiles_update on public.profiles for update to app_backend
  using (id = app.current_profile_id()) with check (id = app.current_profile_id());

-- 기본 분류: 조회만
grant select on public.regions, public.categories, public.service_types to app_backend;
create policy regions_select       on public.regions       for select to app_backend using (true);
create policy categories_select    on public.categories    for select to app_backend using (true);
create policy service_types_select on public.service_types for select to app_backend using (true);

-- shops
grant select, insert on public.shops to app_backend;
grant update (category_id, region_id, name, address, latitude, longitude, phone, description,
  slot_unit_min, min_lead_time_min, late_cancel_hours, approval_timeout_min, auto_confirm, status)
  on public.shops to app_backend;
create policy shops_select on public.shops for select to app_backend
  using (status = 'ACTIVE' or owner_id = app.current_profile_id() or app.is_admin());
create policy shops_insert on public.shops for insert to app_backend
  with check (owner_id = app.current_profile_id() and source = 'INTERNAL');
create policy shops_update on public.shops for update to app_backend
  using (owner_id = app.current_profile_id()) with check (owner_id = app.current_profile_id());

-- 업체 하위 데이터: 공개 업체는 조회, 쓰기는 소유자만 (삭제는 영업시간·휴무만 허용)
grant select, insert, update, delete on public.shop_business_hours, public.shop_closures to app_backend;
grant select, insert, update on public.services, public.staff, public.staff_services to app_backend;
grant select, insert, update, delete on public.staff_work_hours to app_backend;

create policy sbh_select on public.shop_business_hours for select to app_backend using (app.can_view_shop(shop_id));
create policy sbh_write  on public.shop_business_hours for all    to app_backend
  using (app.owns_shop(shop_id)) with check (app.owns_shop(shop_id));
create policy closures_select on public.shop_closures for select to app_backend using (app.can_view_shop(shop_id));
create policy closures_write  on public.shop_closures for all    to app_backend
  using (app.owns_shop(shop_id)) with check (app.owns_shop(shop_id));
create policy services_select on public.services for select to app_backend
  using ((is_active and app.can_view_shop(shop_id)) or app.owns_shop(shop_id));
create policy services_write  on public.services for all to app_backend
  using (app.owns_shop(shop_id)) with check (app.owns_shop(shop_id));
create policy staff_select on public.staff for select to app_backend
  using ((is_active and app.can_view_shop(shop_id)) or app.owns_shop(shop_id));
create policy staff_write  on public.staff for all to app_backend
  using (app.owns_shop(shop_id)) with check (app.owns_shop(shop_id));
create policy staff_services_select on public.staff_services for select to app_backend
  using ((is_active and app.can_view_shop(shop_id)) or app.owns_shop(shop_id));
create policy staff_services_write  on public.staff_services for all to app_backend
  using (app.owns_shop(shop_id)) with check (app.owns_shop(shop_id));
create policy swh_select on public.staff_work_hours for select to app_backend
  using (app.can_view_shop(app.staff_shop_id(staff_id)));
create policy swh_write  on public.staff_work_hours for all to app_backend
  using (app.owns_shop(app.staff_shop_id(staff_id))) with check (app.owns_shop(app.staff_shop_id(staff_id)));

-- reservations: 고객 본인 / 업체 소유자 / 관리자 조회. 전이 규칙은 트리거가 검증
grant select, insert on public.reservations to app_backend;
grant update (staff_id, service_id, start_at, status, reject_reason, canceled_by, cancel_reason, customer_memo)
  on public.reservations to app_backend;
create policy reservations_select on public.reservations for select to app_backend
  using (user_id = app.current_profile_id() or app.owns_shop(shop_id) or app.is_admin());
create policy reservations_insert on public.reservations for insert to app_backend
  with check (user_id = app.current_profile_id() and source = 'INTERNAL');
create policy reservations_update on public.reservations for update to app_backend
  using (user_id = app.current_profile_id() or app.owns_shop(shop_id) or app.is_admin())
  with check (user_id = app.current_profile_id() or app.owns_shop(shop_id) or app.is_admin());

-- reservation_logs: 조회 + 추가만 (수정·삭제 불가)
grant select, insert on public.reservation_logs to app_backend;
create policy rlogs_select on public.reservation_logs for select to app_backend
  using (exists (select 1 from public.reservations r where r.id = reservation_id));
create policy rlogs_insert on public.reservation_logs for insert to app_backend
  with check (actor_user_id = app.current_profile_id()
              and exists (select 1 from public.reservations r where r.id = reservation_id));
