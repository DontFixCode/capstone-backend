#!/usr/bin/env bash
# =====================================================================
# 캡스톤 서버에 Supabase를 직접 설치(Self-Hosting)합니다.
# 공식 가이드 https://supabase.com/docs/guides/self-hosting/docker 의
# "Manual shallow clone" 절차를 그대로 따르고, 캡스톤용 설정만 덧붙입니다.
#
# 사용법 (서버에서, sudo 불필요 / docker 그룹 필요):
#   cd ~/capstone-backend
#   bash selfhost/setup.sh
#
# 옵션 (환경변수):
#   PUBLIC_HOST=121.153.148.189   외부에서 접속할 서버 주소 (기본: 서버 IP 자동)
#   GW_PORT=8000                  Studio/API 포트
#   DB_PORT=5000                  Postgres 포트 (서버 내부 127.0.0.1 전용)
#   SITE_URL=http://localhost:3000  프론트엔드 주소 (로그인 후 이동할 곳)
#   SUPABASE_DIR=~/supabase-project 설치 위치
# =====================================================================
set -euo pipefail

SB_VERSION="self-hosted/v0.8.2"   # 공식 가이드 기준 버전
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${SUPABASE_DIR:-$HOME/supabase-project}"
PUBLIC_HOST="${PUBLIC_HOST:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
GW_PORT="${GW_PORT:-8000}"
DB_PORT="${DB_PORT:-5000}"
SITE_URL="${SITE_URL:-http://localhost:3000}"

say()  { printf '\n\033[1;34m[setup]\033[0m %s\n' "$*"; }
fail() { printf '\n\033[1;31m[setup] 오류:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------
# 0. 사전 점검
# ---------------------------------------------------------------
say "사전 점검"
for cmd in git docker openssl; do
  command -v "$cmd" >/dev/null 2>&1 || fail "'$cmd' 명령이 없습니다."
done
docker ps >/dev/null 2>&1 || fail "docker 권한이 없습니다. 서버 관리자에게 docker 그룹 추가를 요청하세요."
docker compose version >/dev/null 2>&1 || fail "docker compose가 없습니다."
[ -n "$PUBLIC_HOST" ] || fail "서버 주소를 알 수 없습니다. PUBLIC_HOST=서버IP bash selfhost/setup.sh 로 실행하세요."

if [ -f "$TARGET/.env" ]; then
  fail "$TARGET 에 이미 설치되어 있습니다. (비밀키 보호를 위해 덮어쓰지 않습니다)
  - 시작:  cd $TARGET && sh run.sh start
  - 스키마 반영: bash $REPO_DIR/selfhost/migrate.sh"
fi

# 예전에 띄운 단독 PostgreSQL(capstone-db)이 5000번을 쓰고 있으면 끕니다. 데이터 볼륨은 남습니다.
if docker ps --format '{{.Names}}' | grep -qx capstone-db; then
  say "예전 캡스톤 DB 컨테이너(capstone-db)를 중지합니다 (데이터는 보존)"
  docker compose -f "$REPO_DIR/docker-compose.yml" --project-directory "$REPO_DIR" down
fi

port_busy() {
  command -v ss >/dev/null 2>&1 || return 1   # ss가 없으면 점검 생략
  ss -ltnH 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$1\$"
}
port_busy "$GW_PORT" && fail "포트 $GW_PORT 이(가) 사용 중입니다. GW_PORT=8080 처럼 다른 번호로 실행하세요."
port_busy "$DB_PORT" && fail "포트 $DB_PORT 이(가) 사용 중입니다. DB_PORT=5001 처럼 다른 번호로 실행하세요."

mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
if [ "${mem_kb:-0}" -lt 3900000 ]; then
  say "경고: 메모리가 4GB 미만입니다. 공식 최소 사양은 RAM 4GB입니다."
fi

# ---------------------------------------------------------------
# 1. 공식 Docker 파일 받기 (가이드 Option B)
# ---------------------------------------------------------------
say "Supabase 공식 Docker 파일 받기 ($SB_VERSION)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git clone -q --depth 1 --branch "$SB_VERSION" --filter=blob:none --sparse \
  https://github.com/supabase/supabase "$TMP/supabase"
git -C "$TMP/supabase" sparse-checkout set docker
mkdir -p "$TARGET"
cp -rf "$TMP/supabase/docker/." "$TARGET"
cd "$TARGET"
cp .env.example .env
printf 'ref=%s\n' "$SB_VERSION" > .supabase-version
chmod 600 .env

# ---------------------------------------------------------------
# 2. 비밀키 생성 (가이드 3단계)
# ---------------------------------------------------------------
say "비밀키 생성"
sh utils/generate-keys.sh --update-env >/dev/null
sh utils/add-new-auth-keys.sh --update-env >/dev/null
rm -f .env.old

# ---------------------------------------------------------------
# 3. 캡스톤 설정 (가이드 4단계: URL 등)
# ---------------------------------------------------------------
say "주소·포트 설정"
set_env() {  # set_env KEY VALUE  (있으면 교체, 없으면 추가)
  if grep -q "^$1=" .env; then
    sed -i "s|^$1=.*|$1=$2|" .env
  else
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}
set_env SUPABASE_PUBLIC_URL "http://$PUBLIC_HOST:$GW_PORT"
set_env API_EXTERNAL_URL    "http://$PUBLIC_HOST:$GW_PORT/auth/v1"
set_env SITE_URL            "$SITE_URL"
set_env API_GW_HTTP_PORT    "$GW_PORT"
set_env KONG_HTTP_PORT      "$GW_PORT"
set_env POOLER_TENANT_ID    "capstone"
set_env DASHBOARD_USERNAME  "capstone"
set_env CAPSTONE_DB_PORT    "$DB_PORT"

cp "$REPO_DIR/selfhost/docker-compose.capstone.yml" ./docker-compose.capstone.yml
sh run.sh config add capstone >/dev/null

# ---------------------------------------------------------------
# 4. 실행 (가이드 5단계)
# ---------------------------------------------------------------
say "이미지 다운로드 (처음엔 수 분 걸립니다)"
docker compose pull
say "Supabase 시작 (모든 서비스가 healthy가 될 때까지 대기)"
sh run.sh start

# ---------------------------------------------------------------
# 5. 캡스톤 스키마·시드 적용
# ---------------------------------------------------------------
SUPABASE_DIR="$TARGET" bash "$REPO_DIR/selfhost/migrate.sh"

# ---------------------------------------------------------------
# 6. 결과 안내
# ---------------------------------------------------------------
dash_pw=$(grep '^DASHBOARD_PASSWORD=' .env | cut -d= -f2-)
cat <<EOF

=====================================================================
 Supabase 설치 완료!
---------------------------------------------------------------------
 Studio (관리 화면)   http://$PUBLIC_HOST:$GW_PORT
   아이디             capstone
   비밀번호           $dash_pw
 API 주소 (프론트용)  http://$PUBLIC_HOST:$GW_PORT
 공개 키 (프론트용)   sh run.sh secrets  로 SUPABASE_PUBLISHABLE_KEY 확인
 DB (서버 내부 전용)  127.0.0.1:$DB_PORT  (팀원은 SSH 터널로 접속)

 모든 비밀값은 $TARGET/.env 에 있습니다. 절대 GitHub에 올리지 마세요.
 백엔드용 DB 계정 만들기:  bash $REPO_DIR/selfhost/create-api-user.sh
=====================================================================
EOF
