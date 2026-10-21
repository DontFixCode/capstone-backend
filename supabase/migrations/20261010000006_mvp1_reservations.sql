-- 1차 MVP 스키마 (6/10): 예약 · 예약 이력

-- ---------------------------------------------------------------------
-- 8. reservations
-- ---------------------------------------------------------------------
create table public.reservations (
  id                  uuid primary key default gen_random_uuid(),
  user_id             uuid not null references public.profiles(id) on delete restrict,
  shop_id             uuid not null,
  staff_id            uuid not null,
  service_id          uuid not null,
  start_at            timestamptz not null,
  service_end_at      timestamptz not null,
  end_at              timestamptz not null,
  duration_min        integer not null,
  buffer_min          integer not null default 0,
  late_cancel_hours   integer not null,
  status              varchar(20) not null,
  requested_at        timestamptz not null default now(),
  approval_expires_at timestamptz,
  shop_name           varchar(200) not null,
  staff_name          varchar(100) not null,
  service_name        varchar(200) not null,
  price               numeric(12,0) not null,
  customer_memo       text,
  reject_reason       text,
  canceled_by         varchar(20),
  is_late_cancel      boolean not null default false,
  canceled_at         timestamptz,
  cancel_reason       text,
  source              varchar(30) not null default 'INTERNAL',
  external_ref        text,
  version             integer not null default 1,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),

  constraint reservations_staff_service_fk foreign key (shop_id, staff_id, service_id)
    references public.staff_services(shop_id, staff_id, service_id) on delete restrict,
  constraint reservations_id_user_shop_uq unique (id, user_id, shop_id),
  constraint reservations_values_chk check (duration_min > 0 and buffer_min >= 0
    and late_cancel_hours >= 0 and price >= 0 and version >= 1),
  constraint reservations_time_chk check (
    start_at < service_end_at and service_end_at <= end_at
    and service_end_at - start_at = duration_min * interval '1 minute'
    and end_at - service_end_at   = buffer_min   * interval '1 minute'),
  constraint reservations_status_chk check (status in
    ('PENDING','CONFIRMED','REJECTED','CANCELED','COMPLETED','NO_SHOW','EXPIRED')),
  constraint reservations_pending_chk check (status <> 'PENDING' or (
    approval_expires_at is not null
    and requested_at < approval_expires_at and approval_expires_at <= start_at)),
  constraint reservations_canceled_by_chk check (canceled_by is null or canceled_by in ('USER','SHOP','SYSTEM')),
  constraint reservations_cancel_chk check (
    (status = 'CANCELED' and canceled_by is not null and canceled_at is not null and app.is_filled(cancel_reason))
    or (status <> 'CANCELED' and canceled_by is null and canceled_at is null
        and cancel_reason is null and is_late_cancel = false)),
  constraint reservations_late_cancel_chk check (not is_late_cancel or (status = 'CANCELED' and canceled_by = 'USER')),
  constraint reservations_reject_chk check (status <> 'REJECTED' or app.is_filled(reject_reason)),
  constraint reservations_source_chk check (source in ('INTERNAL','NAVER','KAKAO','OTHER')),
  constraint reservations_external_chk check (source <> 'INTERNAL' or external_ref is null),
  constraint reservations_created_chk check (created_at <= requested_at),
  constraint reservations_names_chk check (app.is_filled(shop_name) and app.is_filled(staff_name) and app.is_filled(service_name)),
  -- 같은 담당자의 점유 구간 [start_at, end_at) 중첩 금지
  constraint reservations_no_overlap exclude using gist (
    staff_id with =,
    tstzrange(start_at, end_at, '[)') with &&
  ) where (status in ('PENDING','CONFIRMED','COMPLETED','NO_SHOW'))
);
create index reservations_user_idx    on public.reservations (user_id, start_at desc, id);
create index reservations_shop_idx    on public.reservations (shop_id, start_at, id);
create index reservations_expire_idx  on public.reservations (approval_expires_at) where status = 'PENDING';
create index reservations_service_idx on public.reservations (service_id);
create unique index reservations_external_uq on public.reservations (source, external_ref)
  where external_ref is not null;

create trigger reservations_updated_at before update on public.reservations
  for each row execute function app.set_updated_at();

-- ---------------------------------------------------------------------
-- 9. reservation_logs (append-only 감사 이력)
-- ---------------------------------------------------------------------
create table public.reservation_logs (
  id              uuid primary key default gen_random_uuid(),
  reservation_id  uuid not null references public.reservations(id) on delete restrict,
  action          varchar(30) not null,
  before_snapshot jsonb,
  after_snapshot  jsonb not null,
  actor_type      varchar(20) not null,
  actor_user_id   uuid references public.profiles(id) on delete restrict,
  idempotency_key uuid not null unique,
  request_hash    char(64) not null,
  reason          text,
  created_at      timestamptz not null default now(),
  constraint rlogs_action_chk check (action in
    ('CREATE','APPROVE','REJECT','RESCHEDULE','CANCEL','COMPLETE','NO_SHOW','EXPIRE')),
  constraint rlogs_actor_chk check (actor_type in ('CUSTOMER','SHOP','ADMIN','SYSTEM')),
  constraint rlogs_actor_user_chk check ((actor_type = 'SYSTEM') = (actor_user_id is null)),
  constraint rlogs_before_chk check ((action = 'CREATE') = (before_snapshot is null)),
  constraint rlogs_snapshot_type_chk check (
    jsonb_typeof(after_snapshot) = 'object'
    and (before_snapshot is null or jsonb_typeof(before_snapshot) = 'object')),
  constraint rlogs_snapshot_keys_chk check (
    after_snapshot ?& array['status','start_at','service_end_at','end_at','shop_id','staff_id',
      'service_id','shop_name','staff_name','service_name','price','duration_min','buffer_min',
      'late_cancel_hours','requested_at','approval_expires_at','canceled_by','canceled_at',
      'is_late_cancel','version']
    and (before_snapshot is null or before_snapshot ?& array['status','start_at','end_at','version'])),
  constraint rlogs_version_chk check (
    (action = 'CREATE' and (after_snapshot->>'version')::int = 1)
    or (action <> 'CREATE' and (after_snapshot->>'version')::int = (before_snapshot->>'version')::int + 1)),
  constraint rlogs_reason_chk check (action not in ('REJECT','CANCEL','NO_SHOW') or app.is_filled(reason)),
  constraint rlogs_hash_chk check (request_hash ~ '^[0-9a-f]{64}$')
);
create index rlogs_reservation_idx on public.reservation_logs (reservation_id, created_at, id);
create index rlogs_actor_idx on public.reservation_logs (actor_user_id) where actor_user_id is not null;

create or replace function app.reservation_logs_immutable()
returns trigger language plpgsql set search_path = '' as $$
begin
  raise exception 'reservation_logs는 수정·삭제할 수 없습니다' using errcode = '42501';
end $$;
create trigger reservation_logs_no_update before update or delete on public.reservation_logs
  for each row execute function app.reservation_logs_immutable();
