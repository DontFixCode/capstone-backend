# FastAPI 실행 골격

Python 3.11 이상을 지원하며 Docker는 Python 3.11을 사용한다. 저장소의 기존 uv 방식을 따른다.

저장소 루트에서 실행한다.

```powershell
uv sync --python 3.11
# 선택 사항: 로그 수준 변경이나 TAGO 인증키 입력이 필요할 때 복사한다. 기존 .env.api는 덮어쓰지 않는다.
if (-not (Test-Path .env.api)) { Copy-Item .env.api.example .env.api }
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
TAGO 인증키는 실제 TAGO 호출에만 필요하다(아래 Phase 1 참고). 그 밖의 외부 서비스 설정 예시는 주석으로만 제공한다.
기존 Supabase의 `.env`와 `.env.example`은 유지하며 앱 설정으로 읽지 않는다.

기존 `app/main.py`의 `/db-test`와 `psycopg`, `python-dotenv` 의존성은 사용자 작업이므로 보존했다.
기존 DB 진단 코드는 로컬 `.env`를 읽지만, 실제 DB 연결은 해당 엔드포인트를 호출할 때만 수행한다.
Docker 이미지에는 로컬 환경 파일을 포함하지 않으므로 `/health`는 DB 자격증명 없이 실행된다.
신규 실행 골격에 추가한 의존성은 `pydantic-settings`이고, Phase 1 TAGO 호출을 위해 `httpx`를 추가했다.

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
- `devtools/`: Phase 1 개발용 TAGO 확인 URL(`/dev/transit/...`). 개발 환경에서 명시적으로 켠 경우에만 등록한다.
- `realtime/`: 갱신 결과와 알림 전달.
- `workers/`: 주기 작업 실행 시점과 대상 관리. 업무 규칙은 도메인·워크플로에서 공유할 예정.
- 루트 `migrations/`, `scripts/import_transit.py`, `tests/unit/`, `tests/integration/`, `tests/fixtures/`: 후속 개발 위치.
- 루트 `scripts/inspect_transit.py`(+ `inspect_transit_report.py`): Phase 1 TAGO 응답 확인 CLI.

도메인 파일은 한국어 책임 설명만 포함하며 API 라우터는 빈 상태로 등록했다.
새 기능·WebSocket·인증·DB 모델·알고리즘·수집 작업은 구현하지 않았다.

참고한 현재 문서:
[Introduction](https://www.notion.so/3f6eeac83dec80858f75e770d536adfe),
[Function](https://www.notion.so/3f6eeac83dec801eaa94e003ce22654d).

후속 결정 항목은 DB 스키마·접근 권한, 인증 토큰 검증, 외부 API 공급자와 호출 한도,
이동 상태와 데이터 계약, 평가·추천 기준, 수집 주기와 실시간 전달 계약이다.

## 교통 데이터 확인 (Phase 1)

TAGO 버스 API의 실제 응답·식별자·누락 정보를 DB 없이 확인하는 수동 실행 도구다.
`app/integrations/bus_api.py`가 정류소·노선·위치·도착 7개 상세기능을 호출하고 `scripts/inspect_transit.py`가 계획·저장·요약을 맡는다.
DB 저장·주기 수집·경로 알고리즘은 구현하지 않았다.

### 인증키

```powershell
if (-not (Test-Path .env.api)) { Copy-Item .env.api.example .env.api }
```

`.env.api`의 `TAGO_SERVICE_KEY=` 뒤에 공공데이터포털 마이페이지의 **일반 인증키**(Decoding)를 입력한다.
Encoding 키(`%2B` 등 포함)는 이중 인코딩되므로 실행 전에 거부한다. 키는 httpx가 요청할 때 한 번만 URL 인코딩하며 로그·오류·저장 결과에 남기지 않는다.
설정 우선순위는 프로세스 환경변수 → 저장소 루트 `.env.api` → 기본값이다. 요청 제한시간·간격·재시도는 `BACKEND_TAGO_*` 항목으로 바꾼다.
인증키가 없어도 앱·`/health`·도움말·계획 출력·`analyze`는 실행되며, `--execute`만 키를 확인한다.

### 실행

저장소 루트에서 실행한다. **`--execute`가 없으면 대상·엔드포인트·예상 요청 수만 출력하고 API를 호출하지 않는다.**

```powershell
uv run python -m scripts.inspect_transit --help
uv run python -m scripts.inspect_transit stations                     # 계획만 출력
uv run python -m scripts.inspect_transit stations --execute           # 첫 페이지
uv run python -m scripts.inspect_transit stations --all-pages --max-requests 60 --execute
uv run python -m scripts.inspect_transit routes --all-pages --execute
uv run python -m scripts.inspect_transit route-detail --route-id DJB30300004 --execute
# 아래 경로는 예시다. routes --all-pages로 저장된 실제 실행 디렉터리로 바꾼다.
uv run python -m scripts.inspect_transit route-detail --all-routes --routes-from artifacts\transit-inspection\20261026T010203Z_routes --max-requests 800 --execute
uv run python -m scripts.inspect_transit nearby --lat 36.3504 --lon 127.3845 --execute
uv run python -m scripts.inspect_transit locations --route-id DJB30300004 --execute
uv run python -m scripts.inspect_transit arrivals --node-id DJB8001793 --execute
uv run python -m scripts.inspect_transit analyze
uv run python -m unittest discover -s tests/unit                     # 네트워크 없는 테스트
```

| 모드 | 상세기능 | 범위 |
|---|---|---|
| `stations` / `routes` | getSttnNoList / getRouteNoList | 기본 첫 페이지, 전체 목록은 `--all-pages` |
| `route-detail` | getRouteInfoIem + getRouteAcctoThrghSttnList | `--route-id` 지정 노선, 전체 노선은 `--all-routes` |
| `nearby` | getCrdntPrxmtSttnList | 지정 좌표 |
| `locations` / `arrivals` | getRouteAcctoBusLcList / getSttnAcctoArvlPrearngeInfoList | 지정 노선·정류소만 (전체 실시간 순회 없음) |
| `analyze` | 없음 | 저장된 실행 결과만 사용 |

- `--max-requests`(기본 30)는 재시도를 포함한 실행당 최대 HTTP 요청 수다. 소진되면 호출을 멈추고 확보한 결과와 미완료 대상을 기록한다.
- 요청은 순차 실행한다. 시간초과·연결 오류·HTTP 5xx·게이트웨이 01/05만 재시도하고, 인증 실패·잘못된 요청·호출 한도 초과는 재시도하지 않으며 인증·한도 오류 뒤에는 남은 대상을 호출하지 않는다.
- 전체 페이지 수는 첫 응답의 `totalCount`로 정해지므로 계획에는 확정값 대신 최소 요청 수와 예산을 표시한다.
- `--all-routes`는 노선 목록을 끝까지 조회한 뒤 상세를 호출한다. `--routes-from`에 `routes --all-pages` 실행 디렉터리를 주면 저장된 노선 ID를 쓴다.

### 출력과 요약

결과는 실행마다 `artifacts/transit-inspection/<UTC시각>_<모드>/`에 새로 저장하며 Git에서 제외된다.

- `run.json`: 옵션(인증키 제외), 상태(success·partial·failed·aborted·interrupted), 서비스별 HTTP 시도·재시도·성공·실패, 예산, 미완료 대상, 오류 요약.
- `responses/*.json`: 원본 항목(`items`)과 페이지별 조회 조건·요청 시작/응답 수신 시각(UTC)·`totalCount`·반환 개수·상태·중단 사유.
  응답에 원천 관측 시각이 없으므로 `source_observed_at`은 `null`(미제공)이며 수신 시각으로 채우지 않는다.
- `summary.json`: 아래 검사 결과. `analyze`는 여러 실행을 합쳐 `<UTC시각>_analyze/summary.json`을 만든다.

| 항목 | 해석 |
|---|---|
| `fields` | 필드별 값 있음·0·빈값·null·누락 개수, JSON 타입, 명세 외 필드 |
| `stop_station_linkage` | 경유 정류소 ID와 정류소 목록의 연결. 목록이 일부거나 시외 정류소일 수 있어 미연결을 오류로 단정하지 않음 |
| `route_stop_order` | nodeord 중복·연속성, updowncd 분포, 반복 경유(정류소와 routeId+nodeord 경유 위치를 구분) |
| `bus_locations` | 응답에 routeid가 없어 요청 routeId(조회 맥락)로만 노선·경유 정류소와 연결 |
| `arrivals` | arrtime(초)·남은 정류장 수의 정수 변환. 차량번호가 없어 위치정보 차량과 연결하지 않음 |
| `coordinates` / `nearby` | 좌표 누락·0·범위 밖, 근접 결과의 직선거리(도보 경로·시간 아님)와 citycode |
| `call_volume` | 실제 호출 집계, 정적 하루 1회 갱신·실시간 고정 전체 수집·실시간 필요 대상 수집의 일일 호출량 |

실시간 호출량은 대상별 `ceil(활성 분 / 10)`회(24시간이면 최초 조회 포함 144회) × 대상 수 × 관측 페이지 수다.
대상 수·페이지 수·계정 한도를 모르면 계산식과 미확인 항목만 남긴다. 필요 대상과 한도는 `analyze --location-targets N --arrival-targets M --active-hours H --daily-limit L`로 입력한다.

### 확인 상태

공식 포털 명세와 참고문서를 기준으로 구현했고, 2026-10-26 02시경(KST, 미운행 시간) 실제 호출 12회로 다음을 확인했다.

- 대전(cityCode=25)의 totalCount는 정류소 3,032개, 노선 123개이며 numOfRows=100이 동작한다. 정류소 목록에는 세종 지역 정류소(예: 해밀동)도 있다.
- `nodeno`·`routeno`는 숫자와 문자열이 섞여 온다(예: `"031056"`, `"61-1"`). 앞자리 0은 문자열일 때만 남으므로 ID·번호는 문자열로 다룬다.
  첫차 시각은 `"0540"`(문자열), 막차 시각은 `2235`(숫자)로 형식이 다르다.
- 조회한 2개 노선의 경유 정류소는 nodeord가 1부터 연속이고 updowncd가 모두 있으며 회차 지점에서 한 번 바뀐다.
  61-1번은 정류소 4곳을 같은 nodeid로 상·하행 두 번 경유한다. nodeno는 일부 누락된다.
- 근접 조회에는 대전(25) 외 세종(12) 등 다른 도시 정류소가 섞이고, 같은 실제 정류소가 도시별로 다른 ID(`DJB8001378`, `SJB186005227`)로 나온다.
  명세에 없는 `nodeno`도 응답에 있다.
- 존재하지 않는 routeId도 오류 없이 빈 결과로 온다. 빈 결과만으로는 미운행과 잘못된 ID를 구분할 수 없다.

남은 확인 항목은 운행 시간대 위치·도착 응답(vehicleno·arrtime 형식, 관측 시각 부재), 전체 정류소 목록과 경유 정류소의 ID 연결,
실제 오류 응답 형식, 계정의 실제 일일 한도다.

## 개발용 확인 URL (Phase 1)

브라우저 주소창이나 Swagger에서 조회 조건을 넣고 실제 TAGO 응답을 확인한다. CLI와 같은 `bus_api.py` 클라이언트를 쓴다.

### 활성화와 실행

`.env.api`에 인증키(위 "인증키" 참고)를 넣은 뒤, 실행 환경이 `development`이고 `ENABLE_DEVTOOLS=true`일 때만 URL이 등록된다.
기본값은 `BACKEND_ENV=production`, `ENABLE_DEVTOOLS=false`라 아무것도 설정하지 않으면 등록되지 않는다.
`.env.api`에 두 줄을 넣거나, 현재 터미널에서만 환경변수로 켠다.

```powershell
$env:BACKEND_ENV = "development"; $env:ENABLE_DEVTOOLS = "true"
uv run python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Swagger는 `http://127.0.0.1:8001/docs`의 `devtools: TAGO 확인` 태그에 있다. 인증키가 없으면 앱과 `/health`는 실행되고 확인 URL만 503을 반환한다.

### URL

대전 도시코드 `cityCode=25`는 서버가 넣는다. 목록형 URL은 `page_no`(1 이상, 기본 1)와 `num_of_rows`(1~100, 기본 10)를 받는다.
노선별 경유 정류소만 노선 하나를 한 번에 보도록 `num_of_rows`가 1~500, 기본 200이다.

| URL | 용도·조회 조건 | 예시 |
|---|---|---|
| `/dev/transit/stops` | 정류소 목록. 선택 `stop_name`(nodeNm), `stop_no`(nodeNo) | http://127.0.0.1:8001/dev/transit/stops?stop_name=시청 |
| `/dev/transit/stops/nearby` | 좌표 주변(반경 500m) 정류소. 필수 `lat`, `lon` | http://127.0.0.1:8001/dev/transit/stops/nearby?lat=36.3504&lon=127.3845 |
| `/dev/transit/routes` | 노선 목록. 선택 `route_no` | http://127.0.0.1:8001/dev/transit/routes?route_no=102 |
| `/dev/transit/routes/{route_id}` | 노선 기본정보(페이지 없음) | http://127.0.0.1:8001/dev/transit/routes/DJB30300037 |
| `/dev/transit/routes/{route_id}/stops` | 노선별 경유 정류소(기본 200, 최대 500) | http://127.0.0.1:8001/dev/transit/routes/DJB30300037/stops |
| `/dev/transit/routes/{route_id}/positions` | 노선별 버스 위치 | http://127.0.0.1:8001/dev/transit/routes/DJB30300037/positions |
| `/dev/transit/stops/{stop_id}/arrivals` | 정류소 도착예정정보 | http://127.0.0.1:8001/dev/transit/stops/DJB8001378/arrivals |

ID는 목록 응답에서 얻는다. `routes?route_no=102`의 `raw.response.body.items.item[].routeid`를 노선 URL에,
노선 경유 정류소나 `stops?stop_name=...` 응답의 `nodeid`를 도착정보 URL에 넣는다. 위치·도착은 운행 시간이 아니면 빈 결과이고,
존재하지 않는 ID도 오류 없이 빈 결과로 오므로 ID는 목록에서 복사해 쓴다.

### 응답과 호출 방식

- 응답은 `meta`(TAGO 경로, 인증키를 뺀 조회 조건, 요청·수신 시각(UTC), 제공된 페이지·전체 개수, 외부 HTTP 시도 횟수·상태, resultCode)와
  인증키를 가린 원본 `raw`로 나뉜다. `received_at`은 서버 수신 시각이며 원천 관측 시각이 아니다.
- 요청 한 번에 지정한 대상의 한 페이지만 TAGO에 1회 요청하고 재시도하지 않는다. 다음 페이지는 `page_no`를 올려 다시 요청한다.
- 새로고침·재요청마다 외부 API가 다시 호출되어 개발계정 트래픽이 차감된다. 자동 갱신·저장은 하지 않는다.
- 상태 코드: 입력 오류 422(TAGO 호출 전), 인증키 미설정 503, 제공기관·인증·응답 해석 오류 502, 호출 한도 초과 429, 시간초과 504.

### 비활성화

`ENABLE_DEVTOOLS=false`로 바꾸거나 줄을 지우고(또는 `BACKEND_ENV=production`) 서버를 재시작한다. 환경변수로 켰다면 터미널을 새로 열거나
`Remove-Item Env:ENABLE_DEVTOOLS`를 실행한다. 비활성화하면 URL은 404이고 Swagger·OpenAPI에서도 사라진다.
