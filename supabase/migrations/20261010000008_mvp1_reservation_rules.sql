-- 1차 MVP 스키마 (8/10): 예약 무결성 · 일정 변경 트리거

-- =====================================================================
-- 11. 예약 무결성 트리거
--   - 가격·시간·이름·정책 스냅샷은 DB가 계산 (클라이언트 값 무시)
--   - 업체 행 잠금 → 영업/근무/휴무/슬롯/사전시간 검증
--   - 상태 전이 · 실행 주체 검증
--   중복 점유는 reservations_no_overlap 배제 제약이 최종 차단 (HTTP 409로 변환)
-- =====================================================================
create or replace function app.check_reservation_slot(
  p_shop public.shops, p_staff_id uuid, p_start timestamptz, p_end timestamptz)
returns void language plpgsql stable set search_path = '' as $$
declare
  v_ls  timestamp := p_start at time zone p_shop.timezone;
  v_le  timestamp := p_end   at time zone p_shop.timezone;
  v_dow smallint  := extract(dow from v_ls)::smallint;
begin
  if v_ls::date <> v_le::date then
    raise exception '자정을 넘기는 예약은 지원하지 않습니다' using errcode = '23514';
  end if;
  if exists (select 1 from public.shop_closures c
             where c.shop_id = p_shop.id and c.closed_date = v_ls::date) then
    raise exception '업체 휴무일입니다' using errcode = '23514';
  end if;
  if not exists (
    select 1 from public.shop_business_hours h
    where h.shop_id = p_shop.id and h.day_of_week = v_dow
      and h.open_time <= v_ls::time and v_le::time <= h.close_time
      and (app.time_sec(v_ls::time) - app.time_sec(h.open_time)) % (p_shop.slot_unit_min * 60) = 0) then
    raise exception '영업시간 밖이거나 예약 단위에 맞지 않는 시각입니다' using errcode = '23514';
  end if;
  if not exists (
    select 1 from public.staff_work_hours w
    where w.staff_id = p_staff_id and w.day_of_week = v_dow
      and w.start_time <= v_ls::time and v_le::time <= w.end_time) then
    raise exception '담당자 근무시간 밖입니다' using errcode = '23514';
  end if;
end $$;

-- 현재 메뉴·담당·업체 정책으로 스냅샷 계산 + 활성 여부 검증
create or replace function app.fill_reservation_snapshot(r inout public.reservations, p_shop public.shops)
language plpgsql set search_path = '' as $$
declare
  v_svc   public.services;
  v_staff public.staff;
  v_ss    public.staff_services;
begin
  select * into v_svc   from public.services where id = r.service_id and shop_id = r.shop_id;
  perform app.lock_staff(array[r.staff_id]);
  select * into v_staff from public.staff where id = r.staff_id and shop_id = r.shop_id;
  select * into v_ss    from public.staff_services
    where staff_id = r.staff_id and service_id = r.service_id and shop_id = r.shop_id;

  if v_svc.id is null or not v_svc.is_active then
    raise exception '판매 중인 메뉴가 아닙니다' using errcode = '23514'; end if;
  if v_staff.id is null or not v_staff.is_active then
    raise exception '예약 가능한 담당자가 아닙니다' using errcode = '23514'; end if;
  if v_ss.staff_id is null or not v_ss.is_active then
    raise exception '담당자가 제공하지 않는 메뉴입니다' using errcode = '23514'; end if;

  r.duration_min      := coalesce(v_ss.duration_override_min, v_svc.duration_min);
  r.buffer_min        := v_svc.buffer_min;
  r.price             := coalesce(v_ss.price_override, v_svc.price);
  r.service_end_at    := r.start_at + r.duration_min * interval '1 minute';
  r.end_at            := r.service_end_at + r.buffer_min * interval '1 minute';
  r.late_cancel_hours := p_shop.late_cancel_hours;
  r.shop_name         := p_shop.name;
  r.staff_name        := v_staff.name;
  r.service_name      := v_svc.name;
end $$;

create or replace function app.reservations_before_write()
returns trigger language plpgsql set search_path = '' as $$
declare
  v_shop   public.shops;
  v_me     uuid    := app.current_profile_id();
  v_system boolean := app.is_system();
  v_owner  boolean;
  v_sched  boolean;
begin
  -- 공통 잠금 순서: 업체 행 → 담당자 행
  v_shop := app.lock_shop(coalesce(new.shop_id, old.shop_id));
  if v_shop.id is null then
    raise exception '업체가 존재하지 않습니다' using errcode = '23503';
  end if;
  v_owner := (v_me is not null and v_shop.owner_id = v_me);

  if not v_system and v_me is null then
    raise exception '인증된 활성 회원만 예약을 처리할 수 있습니다' using errcode = '42501';
  end if;

  -- ---------------- INSERT: 신규 예약 ----------------
  if tg_op = 'INSERT' then
    if not v_system and new.user_id is distinct from v_me then
      raise exception '본인 명의로만 예약할 수 있습니다' using errcode = '42501';
    end if;
    if not exists (select 1 from public.profiles where id = new.user_id and status = 'ACTIVE') then
      raise exception '활성 회원이 아닙니다' using errcode = '23514';
    end if;
    if v_shop.status <> 'ACTIVE' then
      raise exception '예약을 받지 않는 업체입니다' using errcode = '23514';
    end if;

    new := app.fill_reservation_snapshot(new, v_shop);
    if new.start_at < now() + v_shop.min_lead_time_min * interval '1 minute' then
      raise exception '예약 가능한 최소 사전 시간이 지났습니다' using errcode = '23514';
    end if;
    perform app.check_reservation_slot(v_shop, new.staff_id, new.start_at, new.end_at);

    new.created_at   := now();
    new.updated_at   := now();
    new.requested_at := now();
    new.version      := 1;
    new.reject_reason := null;
    new.canceled_by := null; new.canceled_at := null; new.cancel_reason := null;
    new.is_late_cancel := false;
    if v_shop.auto_confirm then
      new.status := 'CONFIRMED';
      new.approval_expires_at := null;
    else
      new.status := 'PENDING';
      new.approval_expires_at := least(now() + v_shop.approval_timeout_min * interval '1 minute', new.start_at);
    end if;
    return new;
  end if;

  -- ---------------- UPDATE ----------------
  if new.id <> old.id or new.user_id <> old.user_id or new.shop_id <> old.shop_id
     or new.created_at <> old.created_at or new.source <> old.source
     or new.external_ref is distinct from old.external_ref then
    raise exception '변경할 수 없는 예약 필드입니다' using errcode = '42501';
  end if;
  if old.status in ('REJECTED','CANCELED','COMPLETED','NO_SHOW','EXPIRED') then
    raise exception '종료된 예약은 변경할 수 없습니다 (%)', old.status using errcode = '23514';
  end if;

  v_sched := new.start_at   is distinct from old.start_at
          or new.staff_id   is distinct from old.staff_id
          or new.service_id is distinct from old.service_id;

  if v_sched then
    -- 일정 변경: 고객 본인(또는 시스템)만, 시작 전, 상태는 DB가 결정
    if not v_system and v_me <> old.user_id then
      raise exception '일정 변경은 예약 고객만 할 수 있습니다' using errcode = '42501';
    end if;
    if new.status <> old.status then
      raise exception '일정 변경과 상태 변경은 함께 요청할 수 없습니다' using errcode = '23514';
    end if;
    if now() >= old.start_at then
      raise exception '시작된 예약은 변경할 수 없습니다' using errcode = '23514';
    end if;
    if v_shop.status <> 'ACTIVE' then
      raise exception '예약을 받지 않는 업체입니다' using errcode = '23514';
    end if;
    if new.staff_id <> old.staff_id then  -- 기존 담당자도 ID 순서로 잠금
      perform app.lock_staff(array[old.staff_id, new.staff_id]);
    end if;
    new := app.fill_reservation_snapshot(new, v_shop);
    if new.start_at < now() + v_shop.min_lead_time_min * interval '1 minute' then
      raise exception '예약 가능한 최소 사전 시간이 지났습니다' using errcode = '23514';
    end if;
    perform app.check_reservation_slot(v_shop, new.staff_id, new.start_at, new.end_at);
    new.requested_at := now();
    if v_shop.auto_confirm then
      new.status := 'CONFIRMED';
      new.approval_expires_at := null;
    else
      new.status := 'PENDING';
      new.approval_expires_at := least(now() + v_shop.approval_timeout_min * interval '1 minute', new.start_at);
    end if;
  else
    -- 일정 외 스냅샷은 수정 불가
    if (new.price, new.duration_min, new.buffer_min, new.late_cancel_hours, new.service_end_at,
        new.end_at, new.shop_name, new.staff_name, new.service_name, new.requested_at)
       is distinct from
       (old.price, old.duration_min, old.buffer_min, old.late_cancel_hours, old.service_end_at,
        old.end_at, old.shop_name, old.staff_name, old.service_name, old.requested_at)
       or (new.approval_expires_at is distinct from old.approval_expires_at) then
      raise exception '예약 스냅샷은 직접 수정할 수 없습니다' using errcode = '42501';
    end if;

    if new.status <> old.status then
      case
        when old.status = 'PENDING' and new.status = 'CONFIRMED' then
          if not (v_owner or v_system) then raise exception '업체만 승인할 수 있습니다' using errcode = '42501'; end if;
          if now() >= old.approval_expires_at or now() >= old.start_at then
            raise exception '승인 가능 시간이 지났습니다' using errcode = '23514'; end if;

        when old.status = 'PENDING' and new.status = 'REJECTED' then
          if not (v_owner or v_system) then raise exception '업체만 거절할 수 있습니다' using errcode = '42501'; end if;

        when old.status = 'PENDING' and new.status = 'EXPIRED' then
          if now() < old.approval_expires_at then
            raise exception '아직 승인 마감 전입니다' using errcode = '23514'; end if;

        when old.status in ('PENDING','CONFIRMED') and new.status = 'CANCELED' then
          if new.canceled_by = 'USER' and not (v_me = old.user_id or v_system) then
            raise exception '고객 취소는 예약 고객만 할 수 있습니다' using errcode = '42501';
          elsif new.canceled_by = 'SHOP' and not (v_owner or v_system) then
            raise exception '업체 취소는 업체만 할 수 있습니다' using errcode = '42501';
          elsif new.canceled_by = 'SYSTEM' and not v_system then
            raise exception '시스템 취소 권한이 없습니다' using errcode = '42501';
          end if;
          -- 시작 후 예외 취소는 플랫폼 관리자/시스템만
          if now() >= old.start_at and not (v_system or app.is_admin()) then
            raise exception '시작된 예약은 취소할 수 없습니다' using errcode = '23514';
          end if;
          new.canceled_at    := now();
          new.is_late_cancel := (new.canceled_by = 'USER'
                                 and old.start_at - now() < old.late_cancel_hours * interval '1 hour');

        when old.status = 'CONFIRMED' and new.status = 'COMPLETED' then
          if not (v_owner or v_system) then raise exception '업체만 완료 처리할 수 있습니다' using errcode = '42501'; end if;
          if now() < old.service_end_at then
            raise exception '서비스 종료 후에 완료 처리할 수 있습니다' using errcode = '23514'; end if;

        when old.status = 'CONFIRMED' and new.status = 'NO_SHOW' then
          if not (v_owner or v_system) then raise exception '업체만 노쇼 처리할 수 있습니다' using errcode = '42501'; end if;
          if now() < old.start_at then
            raise exception '예약 시작 후에 노쇼 처리할 수 있습니다' using errcode = '23514'; end if;

        else
          raise exception '허용되지 않는 상태 전이입니다: % → %', old.status, new.status using errcode = '23514';
      end case;
    end if;
  end if;

  new.version := old.version + 1;
  return new;
end $$;

create trigger reservations_before_write
  before insert or update on public.reservations
  for each row execute function app.reservations_before_write();

-- 영업시간·근무시간·휴무 변경 시 기존 활성 예약 충돌 검사 (업체 잠금 후)
create or replace function app.schedule_change_guard()
returns trigger language plpgsql set search_path = '' as $$
declare
  v_shop_id uuid;
  v_bad     uuid;
begin
  if tg_table_name = 'staff_work_hours' then
    v_shop_id := app.staff_shop_id(coalesce(new.staff_id, old.staff_id));
  else
    v_shop_id := coalesce(new.shop_id, old.shop_id);
  end if;
  perform app.lock_shop(v_shop_id);

  if tg_table_name = 'shop_closures' then
    if tg_op = 'DELETE' then return old; end if;
    select r.id into v_bad from public.reservations r
      where r.shop_id = new.shop_id and r.status in ('PENDING','CONFIRMED')
        and (r.start_at at time zone 'Asia/Seoul')::date = new.closed_date
      limit 1;
    if v_bad is not null then
      raise exception '해당 날짜에 진행 중인 예약이 있어 휴무를 등록할 수 없습니다' using errcode = '23P01';
    end if;
    return new;
  end if;

  -- 영업/근무시간: 변경 결과 기준으로 모든 활성 미래 예약이 여전히 들어가는지 확인
  -- (AFTER 트리거에서 실행되므로 새 데이터가 반영된 상태로 검사)
  select r.id into v_bad
  from public.reservations r
  where r.shop_id = v_shop_id and r.status in ('PENDING','CONFIRMED') and r.end_at > now()
    and (
      not exists (select 1 from public.shop_business_hours h
        where h.shop_id = r.shop_id
          and h.day_of_week = extract(dow from r.start_at at time zone 'Asia/Seoul')
          and h.open_time <= (r.start_at at time zone 'Asia/Seoul')::time
          and (r.end_at at time zone 'Asia/Seoul')::time <= h.close_time)
      or not exists (select 1 from public.staff_work_hours w
        where w.staff_id = r.staff_id
          and w.day_of_week = extract(dow from r.start_at at time zone 'Asia/Seoul')
          and w.start_time <= (r.start_at at time zone 'Asia/Seoul')::time
          and (r.end_at at time zone 'Asia/Seoul')::time <= w.end_time))
  limit 1;
  if v_bad is not null then
    raise exception '기존 예약(%)과 충돌하여 시간을 변경할 수 없습니다', v_bad using errcode = '23P01';
  end if;
  return null;
end $$;

create trigger shop_closures_guard before insert or update on public.shop_closures
  for each row execute function app.schedule_change_guard();
create trigger sbh_guard after update or delete on public.shop_business_hours
  for each row execute function app.schedule_change_guard();
create trigger swh_guard after update or delete on public.staff_work_hours
  for each row execute function app.schedule_change_guard();
