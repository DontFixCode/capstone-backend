-- 1차 MVP 스키마 (7/10): 요청 문맥 · 권한 · 잠금 헬퍼

-- =====================================================================
-- 10. 요청 문맥 · 권한 헬퍼
--   FastAPI는 트랜잭션마다 아래처럼 설정합니다 (JWT 검증 후):
--     select set_config('app.auth_user_id', '<jwt sub>', true);
-- =====================================================================
create or replace function app.current_profile_id()
returns uuid language sql stable security definer set search_path = '' as $$
  select p.id from public.profiles p
  where p.auth_user_id = nullif(current_setting('app.auth_user_id', true), '')::uuid
    and p.status = 'ACTIVE'
$$;

create or replace function app.is_admin()
returns boolean language sql stable security definer set search_path = '' as $$
  select coalesce((select p.platform_role = 'ADMIN' from public.profiles p
                   where p.id = app.current_profile_id()), false)
$$;

-- 서버 내부 작업(만료·관리) 문맥 여부. superuser/마이그레이션도 포함
create or replace function app.is_system()
returns boolean language sql stable set search_path = '' as $$
  select pg_catalog.pg_has_role(current_user, 'app_worker', 'MEMBER')
$$;

create or replace function app.owns_shop(p_shop_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
  select exists (select 1 from public.shops s
                 where s.id = p_shop_id and s.owner_id = app.current_profile_id())
$$;

create or replace function app.can_view_shop(p_shop_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
  select exists (select 1 from public.shops s
                 where s.id = p_shop_id
                   and (s.status = 'ACTIVE' or s.owner_id = app.current_profile_id()))
         or app.is_admin()
$$;

create or replace function app.staff_shop_id(p_staff_id uuid)
returns uuid language sql stable security definer set search_path = '' as $$
  select shop_id from public.staff where id = p_staff_id
$$;

-- 잠금 헬퍼: RLS와 무관하게 공통 순서로 행 잠금 (업체 → 담당자 ID 순)
create or replace function app.lock_shop(p_shop_id uuid)
returns public.shops language sql volatile security definer set search_path = '' as $$
  select * from public.shops where id = p_shop_id for update
$$;

create or replace function app.lock_staff(p_ids uuid[])
returns void language plpgsql volatile security definer set search_path = '' as $$
begin
  perform 1 from public.staff where id = any(p_ids) order by id for update;
end $$;

-- 예약 스냅샷(JSON): 로그 before/after에 사용. 개인정보(메모 등)는 제외
create or replace function app.reservation_snapshot(r public.reservations)
returns jsonb language sql stable set search_path = '' as $$
  select jsonb_build_object(
    'status', r.status,
    'start_at', to_char(r.start_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    'service_end_at', to_char(r.service_end_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    'end_at', to_char(r.end_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    'shop_id', r.shop_id, 'staff_id', r.staff_id, 'service_id', r.service_id,
    'shop_name', r.shop_name, 'staff_name', r.staff_name, 'service_name', r.service_name,
    'price', r.price, 'duration_min', r.duration_min, 'buffer_min', r.buffer_min,
    'late_cancel_hours', r.late_cancel_hours,
    'requested_at', to_char(r.requested_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    'approval_expires_at', to_char(r.approval_expires_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    'canceled_by', r.canceled_by,
    'canceled_at', to_char(r.canceled_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    'is_late_cancel', r.is_late_cancel,
    'version', r.version)
$$;
