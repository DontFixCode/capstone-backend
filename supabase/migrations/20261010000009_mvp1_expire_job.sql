-- 1차 MVP 스키마 (9/10): 승인 만료 작업

-- =====================================================================
-- 12. 승인 만료 작업 (app_worker 전용, 1분 간격 실행 권장)
-- =====================================================================
create or replace function app.expire_pending_reservations(p_limit integer default 200)
returns integer language plpgsql set search_path = '' as $$
declare
  r       public.reservations;
  v_new   public.reservations;
  v_count integer := 0;
begin
  if not app.is_system() then
    raise exception 'app_worker 전용 함수입니다' using errcode = '42501';
  end if;
  for r in
    select * from public.reservations
    where status = 'PENDING' and approval_expires_at <= now()
    order by approval_expires_at
    limit p_limit
    for update skip locked
  loop
    update public.reservations set status = 'EXPIRED'
      where id = r.id and status = 'PENDING'
      returning * into v_new;
    if v_new.id is not null then
      insert into public.reservation_logs
        (reservation_id, action, before_snapshot, after_snapshot, actor_type,
         idempotency_key, request_hash, reason)
      values
        (r.id, 'EXPIRE', app.reservation_snapshot(r), app.reservation_snapshot(v_new), 'SYSTEM',
         md5('EXPIRE:' || r.id || ':' || r.version)::uuid,
         encode(extensions.digest('EXPIRE:' || r.id || ':' || r.version, 'sha256'), 'hex'),
         '승인 대기 시간 만료');
      v_count := v_count + 1;
    end if;
  end loop;
  return v_count;
end $$;
