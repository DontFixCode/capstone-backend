#!/usr/bin/env bash
# 아직 적용되지 않은 마이그레이션만 순서대로 적용하고 시드를 넣습니다.
#  - 컨테이너 최초 실행 시 자동 실행 (docker-entrypoint-initdb.d)
#  - 이후 새 마이그레이션 추가 시:  docker compose exec db bash /docker/migrate.sh
set -euo pipefail

SUPA_DIR="${SUPA_DIR:-/supabase}"
DB="${POSTGRES_DB:-postgres}"
USER_NAME="${POSTGRES_USER:-postgres}"
PSQL=(psql -v ON_ERROR_STOP=1 -q -U "$USER_NAME" -d "$DB")

# 컨테이너 밖(로컬 테스트)에서 실행할 때는 PGHOST/PGPORT 환경변수를 사용
"${PSQL[@]}" <<'SQL'
create schema if not exists app_meta;
create table if not exists app_meta.schema_migrations (
  version    text primary key,
  applied_at timestamptz not null default now()
);
SQL

# 자체 PostgreSQL에는 Supabase의 auth 스키마가 없으므로 최소 구조를 먼저 만듭니다.
if [ "$("${PSQL[@]}" -tAc "select to_regclass('auth.users') is null")" = "t" ]; then
  echo "[migrate] auth 스키마 생성 (Supabase 미사용 환경)"
  "${PSQL[@]}" -f "$SUPA_DIR/tests/00_local_supabase_stub.sql"
fi

applied=0
for f in $(ls "$SUPA_DIR"/migrations/*.sql | sort); do
  v="$(basename "$f" .sql)"
  if [ "$("${PSQL[@]}" -tAc "select count(*) from app_meta.schema_migrations where version = '$v'")" = "0" ]; then
    echo "[migrate] 적용: $v"
    # 마이그레이션과 버전 기록을 한 트랜잭션으로
    "${PSQL[@]}" --single-transaction \
      -f "$f" \
      -c "insert into app_meta.schema_migrations (version) values ('$v')"
    applied=$((applied + 1))
  fi
done
echo "[migrate] 새로 적용한 마이그레이션: ${applied}개"

echo "[migrate] 시드 적용 (중복 시 건너뜀)"
"${PSQL[@]}" -f "$SUPA_DIR/seed.sql"
echo "[migrate] 완료"
