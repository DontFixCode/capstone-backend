# FastAPI 실행 골격

Python 3.11 이상을 지원하며 Docker는 Python 3.11을 사용한다. 저장소의 기존 uv 방식을 따른다.

저장소 루트에서 실행한다.

```powershell
uv sync --python 3.11
# 선택 사항: 앱 로그 수준을 변경할 때만 복사한다.
Copy-Item .env.api.example .env.api
uv run python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
```

`GET http://127.0.0.1:8001/health`는 외부 서비스 없이 `{"status":"ok"}`를 반환한다.
API 문서는 `http://127.0.0.1:8001/docs`에서 확인한다. 기존 Supabase의 8000번 포트를 피해 8001번을 사용한다.

```powershell
uv run python -m app.workers.main
```

워커는 실제 작업이 미구현임을 로그로 알리고 정상 종료한다.

```powershell
docker compose -f compose.api.yaml config --quiet
docker compose -f compose.api.yaml up --build -d api
docker compose -f compose.api.yaml --profile worker run --build --rm worker
docker compose -f compose.api.yaml down
```

API Compose 프로젝트는 `capstone-backend`이고 기본 서비스는 API뿐이다. 워커는 `worker` profile에 있으며 자동 재시작하지 않는다.
기존 Supabase Compose와 서비스·네트워크를 공유하거나 변경하지 않는다. DB 초기화와 마이그레이션은 실행하지 않는다.

앱 설정은 실행 디렉터리와 관계없이 저장소 루트의 `.env.api`를 읽는다. 환경 변수 `BACKEND_LOG_LEVEL`이 파일보다 우선하며 기본값은 `INFO`다.
지원 값은 `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`이다. 이 설정은 애플리케이션 로그에 적용된다.
외부 서비스 설정 예시는 주석으로만 제공하며 현재 실행에 필요하지 않다. 기존 Supabase의 `.env`와 `.env.example`은 유지한다.

기존 `app/main.py`의 `/db-test`와 `psycopg`, `python-dotenv` 의존성은 사용자 작업이므로 보존했다.
기존 DB 진단 코드는 로컬 `.env`를 읽지만, 실제 DB 연결은 해당 엔드포인트를 호출할 때만 수행한다.
Docker 이미지에는 로컬 환경 파일을 포함하지 않으므로 `/health`는 DB 자격증명 없이 실행된다.
신규 실행 골격에 추가한 의존성은 `pydantic-settings`뿐이다.

## 구성과 책임

- `core/`: 실행 설정·로그, 추후 보안·공통 예외 처리.
- `db/`: 추후 DB 연결·모델 기반.
- `modules/users/`: 회원정보·탈퇴·사용자 접근 권한.
- `modules/transit/`: 정류장·노선·방향·경유 순서, 정보 정규화·외부 식별자·최신성.
- `modules/journeys/`: 이동 세션·탑승 식별·사용자 확인·상태 전환·이용 결과와 기록.
- `modules/routing/`: 장소 검색 연동·직행 및 최대 1회 환승 후보 생성·실행 불가능 후보 제외.
- `modules/evaluation/`: 예상 도착·대기시간·환승 여유·성공 확률.
- `modules/recommendation/`: 후보 비교·추천 유지 및 변경·추천 이유.
- `workflows/`: 최초 탐색 → 평가 → 추천, 이동 상태 반영 → 필요 시 재탐색 → 재평가 → 추천 갱신.
- `integrations/`: 외부 API 호출·응답 해석.
- `realtime/`: 갱신 결과와 알림 전달.
- `workers/`: 주기 작업 실행 시점과 대상 관리. 업무 규칙은 도메인·워크플로에서 공유할 예정.
- 루트 `migrations/`, `scripts/import_transit.py`, `tests/unit/`, `tests/integration/`, `tests/fixtures/`: 후속 개발 위치.

도메인 파일은 한국어 책임 설명만 포함하며 API 라우터는 빈 상태로 등록했다.
새 기능·WebSocket·인증·DB 모델·알고리즘·수집 작업은 구현하지 않았다.

참고한 현재 문서:
[Introduction](https://www.notion.so/3f6eeac83dec80858f75e770d536adfe),
[Function](https://www.notion.so/3f6eeac83dec801eaa94e003ce22654d).

후속 결정 항목은 DB 스키마·접근 권한, 인증 토큰 검증, 외부 API 공급자와 호출 한도,
이동 상태와 데이터 계약, 평가·추천 기준, 수집 주기와 실시간 전달 계약이다.
