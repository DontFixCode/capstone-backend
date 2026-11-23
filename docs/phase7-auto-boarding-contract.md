# Phase 7 — 자동 탑승 추정 및 알림 연동 계약

## 구현된 자동 탑승 추정

활성 이동의 위치가 새로 수신되면 이동별 GPS 표본을 최대 5분간 저장한다. 워커는 기존 1분 재평가에서 이동 안내가 계산될 때, 선택 경로와 저장된 대안의 버스 위치 수집 결과를 같은 정보 시점으로 읽는다. 사용자 GPS와 버스 위치를 시간·거리로 짝짓고, 연속 표본의 이동 방향과 속도가 함께 맞을 때만 차량 후보를 만든다.

후보는 자동으로 탑승 상태로 전환하지 않는다. 이동 상세와 `/api/v1/journeys/{id}/stream`의 `guidance.boardingEstimate`에 다음 형식으로 제공한다. 상세 API는 재접속 후 최신 안내를 다시 받을 수 있다.

```json
{
  "status": "suggested",
  "reasonCodes": [],
  "confidence": "high",
  "evaluatedAt": "2026-11-23T10:00:00Z",
  "suggestion": {
    "legIndex": 0,
    "routeId": "123",
    "patternKey": "pattern-1",
    "directionCode": "1",
    "vehicleId": "대전xxxx",
    "matchedSamples": 3,
    "meanDistanceM": 18.4,
    "directionAlignment": 0.96,
    "requiresUserConfirmation": true
  }
}
```

`status`는 `suggested`, `no_match`, `insufficient_evidence`, `not_applicable` 중 하나다. GPS 표본·정확도가 부족하면 `need_consecutive_accurate_locations` 또는 `location_stale`, 위치 원천이 없으면 `vehicle_positions_unavailable`, 궤적이 맞지 않으면 `no_vehicle_trajectory_match` 사유를 제공한다. 추정은 확인된 현재 탑승과 별도 필드이며 평가 결과나 사용자 확인 기록으로 저장하지 않는다.

사용자가 확인하거나 수정할 때 기존 `POST /api/v1/journeys/{id}/events`의 `board` 이벤트를 보낸다. `payload.legIndex`는 추정의 `legIndex`, `routeId`는 내부 route ID, `patternKey`와 `vehicleId`는 제안 값을 사용한다. `confirmation: "user_confirmed"`는 사용자가 실제 탑승을 확인했음을 뜻한다. 사용자가 수정한 값이 있으면 제안 대신 수정값을 보낸다. 응답의 새 `stateVersion`과 `Idempotency-Key` 규칙은 기존 이벤트 계약을 따른다.

## 하차 임박·환승 알림 — 프론트엔드 합의 대기

알림을 아직 보내지 않는다. 중복·잘못된 알림이 생기지 않도록 다음 계약을 프론트엔드와 정한 뒤 발생 조건, 상태 저장, 전달 수단을 구현한다.

1. **화면과 수신 시점:** 앱이 foreground일 때 안내 화면의 배너/모달 중 무엇을 쓸지, 화면을 벗어나거나 앱이 종료됐을 때도 보여야 하는지.
2. **하차 임박 기준:** 남은 정류장 수 또는 하차 ETA 기준, 기준을 만족하는 데이터 신선도, 임박 상태가 해제되는 조건.
3. **환승 알림 기준:** 다음 승차 정류소 접근/도착 중 어느 시점인지, 접근 거리·시간, 위치가 오래되거나 실시간 정보가 없을 때 표현.
4. **전달 수단:** 앱 내 UI, 로컬 알림, 푸시 중 지원할 방식과 알림 권한·사용자 설정 처리.
5. **중복·갱신:** 세션/구간별 알림 키, 같은 알림의 갱신·취소, 경로 변경 또는 하차 확인 후 기존 알림 처리.
6. **클라이언트 동작:** `/stream` 이벤트 종류와 payload, 재접속 시 상세 API에서 복구할 현재 알림 상태, 사용자가 알림을 확인/해제하는 동작.

합의 후에는 이 계약에 맞춰 워커의 상태 버전 조건부 저장, `/stream` 메시지, 상세 API 복구 결과를 함께 정의한다. 푸시를 선택하면 토큰 등록·해제와 플랫폼/권한 정책도 별도 합의한다.

## 배포 전 적용

새 데이터베이스에는 기존 마이그레이션 다음으로 `migrations/12_journey_presence.sql`을 적용한다. GPS 이력 테이블은 journey 삭제 시 cascade 삭제되고, 삽입 시 5분보다 오래된 표본은 정리된다. 자동 추정의 실제 현장 정확도는 연속 위치와 버스 위치 데이터가 확보된 기기 주행으로 별도 확인해야 한다.
