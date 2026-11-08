"""도착정보 잔여초의 ETA 기준 시각 정책(Notion 요구사항명세서 DB-C04).

절대 ETA는 확인한 기준 시각 또는 검증한 근사 정책으로만 만든다. TAGO 기본 도착정보는 기준 시각을 주지 않으므로
기본 정책(unconfirmed)에서는 절대 ETA를 만들지 않는다. server_received_at 정책은 서버 응답 수신 시각(collected_at)을
기준으로 근사하고 출처를 server_received_at_approx로 표시한다. 수신 시각을 원천 생성 시각(source_generated_at)으로
대신 쓰지 않으며, DB의 eta_basis_at·predicted_arrival_at 원값은 바꾸지 않고 평가·응답 시점에만 적용한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

UNCONFIRMED = "unconfirmed"
SERVER_RECEIVED_AT = "server_received_at"
POLICY_VERSIONS = {
    UNCONFIRMED: "tago-arrival-basis-unconfirmed/1",
    SERVER_RECEIVED_AT: "tago-arrival-basis-server-received-at/1",
}
APPROX_SOURCE = "server_received_at_approx"
PROVIDER_SOURCE = "provider"


def policy_version(policy: str) -> str:
    return POLICY_VERSIONS[policy]


def resolve_basis(obs: dict[str, Any], policy: str) -> tuple[datetime | None, str | None]:
    """관측 한 건의 (ETA 기준 시각, 기준 출처). 확인된 기준이 있으면 그것을, 없으면 정책에 따라 근사하거나 (None, None)."""
    if obs.get("eta_basis_at") is not None:
        return obs["eta_basis_at"], (obs.get("details") or {}).get("eta_basis_source") or PROVIDER_SOURCE
    if policy == SERVER_RECEIVED_AT and obs.get("remaining_seconds") is not None and obs.get("collected_at"):
        return obs["collected_at"], APPROX_SOURCE
    return None, None


def predicted_arrival(obs: dict[str, Any], basis_at: datetime | None) -> datetime | None:
    """저장된 절대 ETA가 있으면 그 값, 없으면 기준 시각 + 잔여초. 기준이 없으면 None."""
    if obs.get("predicted_arrival_at") is not None:
        return obs["predicted_arrival_at"]
    if basis_at is None or obs.get("remaining_seconds") is None:
        return None
    return basis_at + timedelta(seconds=obs["remaining_seconds"])
