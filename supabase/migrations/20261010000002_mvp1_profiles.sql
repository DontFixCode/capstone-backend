-- 1차 MVP 스키마 (2/10): 회원 프로필 · Auth 가입 트리거

-- ---------------------------------------------------------------------
-- 2. profiles
-- ---------------------------------------------------------------------
create table public.profiles (
  id            uuid primary key default gen_random_uuid(),
  auth_user_id  uuid unique references auth.users(id) on delete set null,
  name          varchar(100),
  phone         varchar(30),
  platform_role varchar(20) not null default 'USER',
  status        varchar(20) not null default 'ACTIVE',
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  deleted_at    timestamptz,
  constraint profiles_role_chk   check (platform_role in ('USER','ADMIN')),
  constraint profiles_status_chk check (status in ('ACTIVE','SUSPENDED','DELETED')),
  constraint profiles_active_required_chk
    check (status = 'DELETED' or (auth_user_id is not null and app.is_filled(name))),
  constraint profiles_deleted_at_chk
    check ((status = 'DELETED') = (deleted_at is not null))
);

create trigger profiles_updated_at before update on public.profiles
  for each row execute function app.set_updated_at();

-- Auth 가입 → 프로필 자동 생성. 권한은 메타데이터와 무관하게 USER 고정.
create or replace function app.handle_new_auth_user()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  v_name text := nullif(btrim(coalesce(new.raw_user_meta_data->>'name', '')), '');
begin
  insert into public.profiles (auth_user_id, name, phone, platform_role, status)
  values (
    new.id,
    left(coalesce(v_name, nullif(split_part(coalesce(new.email, ''), '@', 1), ''), '회원'), 100),
    left(nullif(btrim(coalesce(new.raw_user_meta_data->>'phone', '')), ''), 30),
    'USER',
    'ACTIVE'
  )
  on conflict (auth_user_id) do nothing;
  return new;
end $$;
revoke all on function app.handle_new_auth_user() from public;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function app.handle_new_auth_user();
