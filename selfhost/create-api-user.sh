#!/usr/bin/env bash
# FastAPI 백엔드용 DB 계정(api_server)을 만들고 접속 주소를 출력합니다.
#   bash selfhost/create-api-user.sh
# 이미 있으면 비밀번호만 새로 바꿉니다. 출력된 주소는 개인 메시지로만 공유하세요.
set -euo pipefail

TARGET="${SUPABASE_DIR:-$HOME/supabase-project}"
cd "$TARGET"
DB_PORT="$(grep '^CAPSTONE_DB_PORT=' .env | cut -d= -f2- || true)"
DB_PORT="${DB_PORT:-5000}"
PW="$(openssl rand -hex 24)"

docker compose exec -T db psql -U supabase_admin -d postgres -v ON_ERROR_STOP=1 -q <<SQL
do \$\$ begin
  if exists (select 1 from pg_roles where rolname = 'api_server') then
    alter role api_server with login password '$PW';
  else
    create role api_server login password '$PW';
  end if;
end \$\$;
grant app_backend to api_server;
SQL

cat <<EOF

[api_server] 백엔드용 DB 계정 준비 완료

  같은 서버에서 실행하는 FastAPI:
    DATABASE_URL=postgresql://api_server:$PW@127.0.0.1:$DB_PORT/postgres

  내 컴퓨터에서 개발할 때 (먼저 SSH 터널 열기):
    ssh -L 15000:127.0.0.1:$DB_PORT <계정>@<서버IP> -p 65522
    DATABASE_URL=postgresql://api_server:$PW@127.0.0.1:15000/postgres

  ⚠️ 이 주소는 개인 메시지로만 공유하고 GitHub에 올리지 마세요.
EOF
