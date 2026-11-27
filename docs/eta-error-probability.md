# 환승 여유시간 등급과 단일 차량 ETA 오차 결합 확률

환승 성공 확률은 원래 "같은 상황에서 짝지은 두 차량의 상대 오차 표본"(판정된 `app.transfer_samples`)으로만 낸다. 대전시 도착
예측에는 운행 회차 키가 없어 그 표본이 만들어지지 않으므로, 모든 환승의 확률이 `null`이었다. 여기서는 두 가지를 더한다.

1. **여유시간 등급**: 확률이 없어도 시간이 계산된 모든 환승에 등급을 준다.
2. **단일 차량 오차 결합 확률**: 버스 한 대씩의 "실제 도착 − 예측 도착" 오차를 워커가 따로 모으고, 앞 차량과 다음 차량의 두
   분포를 결합해 확률 범위를 낸다. 기존 짝 표본 방식 다음 순서로 시도한다.

새 동작은 모두 설정으로 켜며 기본은 꺼짐이다. 임계값·최소 표본 수 같은 수치는 코드 기본값이 없고 `.env.api`에서만 받는다.

## 응답에 추가된 필드

`evaluation.transfers[]`(검색·이동 응답)에 아래가 추가된다. 기존 필드의 이름과 뜻은 그대로다.

| 필드 | 값 |
|---|---|
| `slackGrade` | `missed`(여유 < 0) / `tight`(0 이상 하한 미만) / `moderate`(하한 이상 상한 미만) / `comfortable`(상한 이상) / `null`(시각 미확인 또는 경계 설정 없음) |
| `slackEstimated` | 앞 차량 하차 ETA·다음 차량 승차 ETA 중 하나라도 제공처 예측이 아니면 `true`, 여유를 계산하지 못했으면 `null` |
| `probabilitySource` | `paired_ecdf`(짝 표본) / `marginal_ecdf`(단일 차량 오차 결합) / `null`(확률 없음) |
| `probabilityRange` | `[하한, 상한]` 또는 `null`. `paired_ecdf`는 두 값이 같다 |
| `probabilitySampleCounts` | 선정한 표본 수. `paired`(짝 표본), `front`·`next`(앞·다음 차량의 단일 차량 표본) |

- `probability`는 `marginal_ecdf`일 때 범위의 **하한**이다.
- 사유 코드가 환승의 `reasonCodes`와 후보의 `evaluation.reasonCodes`에 들어간다.
  - `transfer_probability_by_marginal_errors`: 단일 차량 오차 결합으로 확률을 냈다.
  - `marginal_error_samples_insufficient`: 앞 또는 다음 차량의 표본이 최소 수에 못 미친다.
  - `marginal_error_source_unsupported`: 표본을 쌓지 않는 방식의 예측이고(`leading_vehicle_gap`, `headway_estimate`,
    `historical_segments`) 근사도 허용하지 않았다.
  - `marginal_error_source_approximated`: 표본을 쌓지 않는 방식의 예측에 제공처 예측의 오차 분포를 빌려 썼다(근사).
    위의 두 코드 중 하나와 함께 나온다.
- 같은 값이 후보의 `evaluation` JSON(`slack_grade`, `probability_source` 등), `evaluation_snapshot` 이벤트,
  `transfer_samples.probability_context`에 남는다.
- `journeyProbability`와 `expectedTotalSeconds`의 계산은 바꾸지 않았다.

## 확률 계산

환승마다 순서대로 시도한다.

1. 짝 표본(`paired_ecdf`): 기존 계산과 표본 선정 그대로다.
2. 단일 차량 오차 결합(`marginal_ecdf`): `BACKEND_EVAL_MARGINAL_PROBABILITY_ENABLED=true`일 때만.
   - 앞 차량 예측과 다음 차량 예측 각각에 대해 생성 방식(`eta_source`)·모델 버전·기준 시각 출처·잔여시간 구간이 같은
     표본을 최근 `BACKEND_EVAL_MARGINAL_WINDOW_DAYS`일에서 고른다. 양쪽 모두 `BACKEND_EVAL_MIN_MARGINAL_SAMPLES` 이상이어야 한다.
   - 여유 m에 대해, 앞 표본 i와 다음 표본 j의 모든 조합에서
     - 하한 = (앞 오차 상한ᵢ − 다음 오차 하한ⱼ ≤ m 인 조합 수) ÷ 전체 조합 수
     - 상한 = (앞 오차 하한ᵢ − 다음 오차 상한ⱼ ≤ m 인 조합 수) ÷ 전체 조합 수
   - 예: 앞 오차 0·60·180초, 다음 오차 0·120초, m = 120초이면 6개 조합 중 5개가 연결되어 5/6이다.
   - 두 차량의 오차가 서로 독립이라는 가정 아래의 값이다.
   - **근사**(`BACKEND_EVAL_MARGINAL_APPROXIMATE_SOURCES`, 기본 없음): 오차 표본은 제공처 예측(`provider`)에만 쌓인다.
     여기에 적은 생성 방식의 예측은 같은 잔여시간 구간의 제공처 예측 표본을 빌려 같은 식으로 계산하고,
     `marginal_error_source_approximated`를 남긴다. 다른 방식의 오차를 대신 쓰는 것이라 실제 오차가 더 큰 방식은 확률이
     실제보다 높게 나온다. `leading_vehicle_gap`은 제공처 잔여시간 두 개를 더한 값이라 차이가 작을 것으로 보지만,
     `headway_estimate`는 배차간격이 실제 출발 간격과 수 분 어긋날 수 있어 차이가 크다. 적지 않은 방식이 낀 환승은
     확률을 내지 않는다.
3. 없음(`null`).

2026-11-27 서버 기록(11월 20일 이후 평가한 후보의 환승 1,142건)에서 앞·다음 예측이 모두 제공처 예측인 환승은 32건(약 3%)
이었다. 나머지는 앞 차량 하차 ETA나 다음 차량 승차 ETA가 `leading_vehicle_gap` 또는 `headway_estimate`다. 근사를 켜지 않으면
환승 대부분은 확률이 `null`이다.

표본은 노선·정류소별로 나누지 않고 합쳐 쓴다. 그래서 수집 대상에 없는 노선의 환승에도 확률이 나오지만, 그 노선의 오차가
수집한 노선과 비슷하다는 가정이 들어간다.

## 오차 표본 수집(워커)

`BACKEND_ETA_ERROR_SAMPLING_ENABLED=true`이면 워커가 수집 시간대(KST) 동안 다음을 설정한 주기로 받는다. 이 대상만 짧은
주기로 돌고 나머지 대상은 `BACKEND_REALTIME_INTERVAL_SECONDS`(600초) 그대로다.

- 대상 정류소의 도착정보(`getArrInfoByStopID`, 기존 수집 경로): 예측을 얻는다.
- 대상 노선의 위치(노선당 호출 1회에 운행 중인 모든 차량): 도착 시각을 얻는다. 원천은
  `BACKEND_ETA_ERROR_POSITION_SOURCE`로 고른다.

| 원천 | API | 도착 시각 | 일일 한도 |
|---|---|---|---|
| `daejeon`(기본) | 대전시 `getBusPosByRtid` | 정류소 이벤트 시각(`ARR_TIME`)으로 잰다 | 이 계정은 100건(아래) |
| `tago` | TAGO `getRouteAcctoBusLcList` | 시각이 없어 앞뒤 조회 시각 사이의 범위로만 안다 | 2,725건까지 받은 날이 있고 한도 오류는 없었다 |

**대전시 노선 위치의 일일 한도는 100건이다.** 2026-11-27에 서버에서 노선 16개를 90초 주기로 켰더니 9분 만에 100건째 호출이
HTTP 429, 게이트웨이 코드 22(`LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR`)로 거절됐다. 도착정보는 같은 날 계속
받았다. 한도를 올리기 전에는 `daejeon` 원천으로 표본을 모을 수 없어 서버는 `tago` 원천을 쓴다.

운행 분리(두 원천 공통): 경유 순번이 줄거나, 정류소 방향(`route_stops.direction_code`)이 바뀌거나, 직전 기록과의 간격이
`BACKEND_ETA_ERROR_RUN_GAP_SECONDS`를 넘으면 새 운행이다. 한 방향의 운행 하나가 운행 1회다. 저장할 때 운행 키
(`details.run_group`)를 정해 함께 남긴다.

### 원천이 `tago`일 때

- 대상 노선의 TAGO 위치 조회를 설정한 주기로 받는다. 검색·혼잡도가 등록한 같은 노선과는 한 대상으로 합쳐 한 번만 부른다.
- **위치 기록**: 조회마다 응답의 모든 차량을 `transit_observations`(`kind=position`)에 남기고(기존 TAGO 위치 행 그대로, 좌표
  포함) 운행 키를 더한다.
- **도착 이벤트**: 차량이 처음으로 대상 순번 이상에서 확인된 조회와 그 직전 조회(대상보다 앞 순번)를 찾는다. 도착은
  (직전 조회를 보낸 시각, 이번 응답을 받은 시각] 범위(`bounded`)다. 조회 시각은 범위의 양 끝으로만 쓰고 도착 시각 자체로
  쓰지 않는다. 직전 조회가 없으면(처음 확인됐을 때 이미 대상 이상) 만들지 않는다. 이벤트 키는
  `tago-position-arrival:{운행}:{순번}`이다.
- 범위의 폭은 수집 주기 정도다(차량이 빠진 조회가 있으면 그만큼 넓다).
- 하한은 TAGO가 실제 위치를 지연 없이 반영한다고 본 값이다. 지연이 있으면 실제 도착은 하한보다 그만큼 이를 수 있다.

### 원천이 `daejeon`일 때

**노선 위치 응답**(2026-11-27 06:07 KST 실조회, 608번 `busRouteId=30300130`, 차량 4대): 도착정보와 같은 틀
(`ServiceResult > msgHeader·msgBody > itemList`)이고 항목은 `ARR_TIME`, `BUS_NODE_ID`, `BUS_STOP_ID`, `DIR`, `EVT_CD`,
`GPS_LATI`, `GPS_LONG`, `PLATE_NO`, `ROUTE_CD`, `STRE_DT`, `TOTAL_DIST`, `ud_type`다.

- `ARR_TIME`은 `yyyymmddhhmmss`이고 조회 시각보다 97~147초 이른 KST 시각이었다. 조회 시점의 시각이 아니라 정류소에서 생긴
  이벤트의 시각으로 보인다. `STRE_DT`는 `ARR_TIME`과 같거나 1~3초 늦었다.
- `EVT_CD`는 네 대 모두 `1`이었다.
- 운행 차량이 없으면(같은 날 05:32 KST) `headerCd=4`, `itemCnt=0`, 빈 `msgBody`가 온다.

**위치 기록**: 차량의 정류소(`BUS_NODE_ID`)가 바뀐 때만 `transit_observations`(`kind=position`)에 저장한다. 좌표는
채우지 않는다(GPS가 어느 시점의 위치인지 확인 전).

**도착 이벤트**(`kind=event`, `event_type=arrival`): 한 운행의 기록을 수집 순으로 놓고 대상 정류소마다

| 상황 | 결과 |
|---|---|
| 대상 정류소 기록이 있고 `ARR_TIME`의 뜻이 `arrival` | 그 `ARR_TIME`을 점 시각(`exact`)으로 |
| 뜻이 `departure` | [직전 기록, 대상 정류소 기록] 범위(`bounded`) |
| 뜻이 `unverified`이거나 대상 정류소 기록을 놓침 | [직전 기록, 다음 기록] 범위(`bounded`) |
| 필요한 앞뒤 기록이 없음 | 만들지 않음 |

- 직전·다음 기록은 같은 운행에서 대상보다 앞·뒤 순번인 가장 가까운 기록이다.
- 한 패턴에서 두 번 지나는 정류소는 순번을 정할 수 없어 대상에서 뺀다.
- 범위로만 아는 도착을 대표 시각으로 바꾸지 않고, 수집 시각을 도착 시각으로 쓰지 않는다.
- `run_key` 컬럼은 비워 둔다. 기존 구간 소요시간과 사후 판정은 `run_key`가 있는 이벤트만 읽으므로 영향을 받지 않는다.

### 오차 표본(두 원천 공통)

**오차 표본**(`app.eta_error_samples`): 도착 이벤트마다 같은 차량번호·정류소·노선의 이전 도착 예측을 잇는다.

- `MSG_TP=07`(다음 차량 출발 전) 항목은 예측이 아니므로 뺀다.
- 예측 기준 시각이 도착 상한보다 늦거나, 그 사이가 `BACKEND_ETA_ERROR_MATCH_WINDOW_SECONDS`를 넘으면 잇지 않는다.
- 잔여시간을 `BACKEND_ETA_ERROR_HORIZON_EDGES_SECONDS`로 구간화하고, 한 운행에서 구간마다 가장 이른 예측 하나만 쓴다.
- 고유 키는 (운행, 생성 방식, 모델 버전, 잔여시간 구간)이다. 한 운행이 대상 정류소 여러 곳을 지나도 구간당 표본은 하나다.
- 어느 원천으로 잰 도착인지는 `details.position_source`·`details.event_rule`에 남는다. 확률 계산은 둘을 구분하지 않는다.

**저장량**: 표본 수집 대상의 정상 응답은 `api_fetches.raw_body`를 남기지 않는다(오류 응답만 남김). 항목 원값은 관측 행의
`details.source`에 있다. 도착정보 관측 행은 정류소의 모든 노선이 조회마다 저장된다. 그 스냅샷을 검색도 읽으므로 대상 노선만
남길 수 없다. `tago` 원천은 노선 위치도 조회마다 모든 차량이 행으로 남는다(노선 16개·90초·7시간이면 하루 약 4,500회 조회,
행은 차량 수만큼).

**호출 한도 보호**: 서비스(도착정보·노선 위치)의 그날(KST) 호출 수가 `BACKEND_ETA_ERROR_SAMPLING_DAILY_CALL_LIMIT`
(노선 위치는 `..._POSITION_DAILY_CALL_LIMIT`이 있으면 그 값)에 닿으면 표본 수집 대상을 모두 뺀다. 상한과 별개로, 그날 그
서비스가 한도 초과로 거절된 적이 있으면(HTTP 429 또는 코드 22) 남은 시간에는 호출하지 않는다. `tago` 원천은 검색·안내·
혼잡도와 같은 TAGO 노선 위치 한도를 쓰므로, 한도를 넘기면 그 기능들도 그날 위치를 받지 못한다.

## 수집 주기와 호출량

도착 시각의 정밀도는 노선 위치 조회 주기가 정한다. 차량이 대상 정류소에 있는 순간을 잡지 못하면 앞뒤 기록 사이의 범위로만
남고, 그 폭은 대략 조회 주기와 같거나 더 넓다. 범위가 넓으면 확률 범위가 넓어지고 `probability`(하한)가 낮아진다.

대상 1개당 하루 호출 = 수집 시간대 길이 ÷ 주기. 06~23시(17시간)면 60초에 1,020회, 120초에 510회, 180초에 340회다.

| 노선 수 | 60초 | 120초 | 180초 |
|---|---|---|---|
| 5 | 5,100 | 2,550 | 1,700 |
| 8 | 8,160 | 4,080 | 2,720 |
| 20 | 20,400 | 10,200 | 6,800 |

정류소는 호출 1회에 그 정류소의 모든 노선이 오므로 대상 노선이 지나는 몇 곳이면 된다. 도착정보 한도는 검색·이동 안내와 같이
쓴다. 표의 호출량은 노선 위치 원천의 한도 안에 있어야 한다(`daejeon`은 하루 100건이라 표의 어느 칸도 들어가지 않는다).

2026-11-27 서버 실측(대전시 원천, 노선 16개·정류소 7곳, 90초, 9분): 노선 위치 98건, 위치 기록 788건, 도착 이벤트 17건,
표본 25개(운행 15회). 도착 범위의 폭은 중앙값 230~250초였다.

## 설정

| 설정 | 예시 | 필요 조건 |
|---|---|---|
| `BACKEND_EVAL_SLACK_GRADE_THRESHOLDS_SECONDS` | `120,300` | 비우면 등급 `null` |
| `BACKEND_EVAL_MARGINAL_PROBABILITY_ENABLED` | `false` | 기본 꺼짐 |
| `BACKEND_EVAL_MARGINAL_WINDOW_DAYS` | `14` | 확률 결합을 켜면 필수 |
| `BACKEND_EVAL_MIN_MARGINAL_SAMPLES` | `30` | 확률 결합을 켜면 필수 |
| `BACKEND_EVAL_MARGINAL_APPROXIMATE_SOURCES` | `leading_vehicle_gap,headway_estimate` | 선택(비우면 근사하지 않음) |
| `BACKEND_ETA_ERROR_HORIZON_EDGES_SECONDS` | `120,300,600` | 수집 또는 확률 결합을 켜면 필수 |
| `BACKEND_ETA_ERROR_SAMPLING_ENABLED` | `false` | 기본 꺼짐 |
| `BACKEND_ETA_ERROR_SAMPLING_STOPS` | `DJB8001488` | 수집을 켜면 필수 |
| `BACKEND_ETA_ERROR_SAMPLING_ROUTES` | `DJB30300130` | 수집을 켜면 필수 |
| `BACKEND_ETA_ERROR_SAMPLING_INTERVAL_SECONDS` | `60` | 수집을 켜면 필수 |
| `BACKEND_ETA_ERROR_SAMPLING_HOURS` | `6-23` | 수집을 켜면 필수(KST, 끝 시각 제외) |
| `BACKEND_ETA_ERROR_MATCH_WINDOW_SECONDS` | `1800` | 수집을 켜면 필수 |
| `BACKEND_ETA_ERROR_RUN_GAP_SECONDS` | `600` | 수집을 켜면 필수, 수집 주기보다 커야 함 |
| `BACKEND_ETA_ERROR_POSITION_SOURCE` | `tago` | 기본 `daejeon`(일일 100건 한도) |
| `BACKEND_ETA_ERROR_SAMPLING_DAILY_CALL_LIMIT` | `5000` | 선택(비우면 호출 수로는 제한 없음) |
| `BACKEND_ETA_ERROR_SAMPLING_POSITION_DAILY_CALL_LIMIT` | `7000` | 선택(비우면 위 값을 노선 위치에도 씀) |
| `BACKEND_DAEJEON_POSITION_ARR_TIME_MEANING` | `unverified` | 기본 `unverified`, `daejeon` 원천에만 쓰임 |

켰는데 필수 값이 빠지면 누락된 키 이름을 모아 설정 오류로 처리한다. 표본 수집은 그 회차의 표본 수집만 건너뛰고(기존 수집은
계속), 확률 결합은 검색이 설정 오류로 멈춘다.

## 서버에서 켜는 순서

1. `migrations/14_eta_error_samples.sql`을 대상 DB에 적용한다(새 테이블 `app.eta_error_samples` 하나, 기존 테이블 변경 없음).
   적용 전에 수집을 켜면 표본 수집 단계가 `eta_error_samples_table_missing`으로 건너뛴다.
2. 코드를 배포하고 워커를 다시 시작한다.
3. `.env.api`에 `BACKEND_EVAL_SLACK_GRADE_THRESHOLDS_SECONDS`를 넣는다(등급은 표본 없이 바로 나온다).
4. 수집 설정을 넣고 `BACKEND_ETA_ERROR_SAMPLING_ENABLED=true`로 켠다. 워커는 설정을 주기마다 다시 읽는다.
5. `uv run python -m scripts.eta_error_status --days 1`로 위치 기록·도착 이벤트·표본이 쌓이는지 본다.
6. 구간별 표본이 최소 수를 넘으면 `BACKEND_EVAL_MARGINAL_PROBABILITY_ENABLED=true`와 창·최소 표본 수를 넣는다. API 서버와
   워커가 같은 `.env.api`를 읽어야 한다.

## 확인하지 못한 것

- `ARR_TIME`이 그 정류소 도착 시각인지 출발 시각인지, `EVT_CD`의 뜻. 그래서 기본은 `unverified`이고 도착은 범위로만 남는다.
  실응답은 두 번만 조회했다(형식 확인용).
- 노선 위치 조회의 `headerCd=4`("결과가 없습니다.")는 운행 차량이 없을 때 오는 것으로 보고 빈 결과로 처리한다. 잘못된 노선
  ID와는 응답으로 구분되지 않는다.
- 대전시 한도의 집계 기준 시각이 KST 0시인지. 한도는 서비스별로 다르다(노선 위치 100건은 실측, 도착정보 10,000건은
  사용자 확인).
- TAGO 노선 위치의 일일 한도(2,725건까지 받은 날이 있다는 것만 안다)와, TAGO가 실제 위치를 반영하는 지연.
- TAGO의 `nodeord`가 '가장 최근에 도착한 정류소'인지 '향하는 다음 정류소'인지. 뒤쪽이면 도착을 한 구간만큼 이르게 잰다.
- 경유 순번이 일시적으로 줄어드는 응답이 있는지(있으면 한 운행이 둘로 나뉜다).
- 두 차량의 오차가 독립이라는 가정과, 노선·정류소를 합친 분포가 개별 환승에 맞는지.
- 근사를 켰을 때 `leading_vehicle_gap`·`headway_estimate` 예측의 실제 오차가 제공처 예측과 얼마나 다른지(그 방식의 오차
  표본은 아직 모으지 않는다).
