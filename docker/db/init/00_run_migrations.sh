#!/usr/bin/env bash
# postgres 이미지가 빈 데이터 볼륨으로 처음 시작할 때 한 번 실행됩니다.
set -euo pipefail
bash /docker/migrate.sh
