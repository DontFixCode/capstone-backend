"""추천 결과와 추천·제외 이유의 데이터 형식을 정의한다. 변경 판단 형식은 이동 관리 구현 때 추가한다."""

from __future__ import annotations

from typing import Literal

from app.modules.evaluation.schemas import ConnectionStatus, Probability, TimeStatus
from app.modules.routing.schemas import FrozenSchema

RankingMode = Literal["nominal", "expected"]


class RankedCandidate(FrozenSchema):
    """순위에 포함된 후보. 추천 비용과 성공 확률은 서로 다른 값이다."""

    rank: int
    static_candidate_id: str
    timed_candidate_id: str
    ranking_mode: RankingMode
    time_seconds: float
    user_bias_seconds: float
    equilibrium_bias_seconds: float
    cost_seconds: float
    transfer_count: int
    walk_seconds: float
    first_boarding_probability: Probability | None
    transfer_probabilities: tuple[Probability | None, ...]
    journey_probability: Probability | None
    reason_codes: tuple[str, ...]


class ExcludedCandidate(FrozenSchema):
    """순위에서 제외한 후보와 이유. 평가 제한 목록이므로 ranking_mode는 항상 None이다."""

    static_candidate_id: str
    timed_candidate_id: str
    ranking_mode: None = None
    time_status: TimeStatus
    nominally_feasible: bool | None
    connection_status: ConnectionStatus
    limited_at_ride_index: int | None
    reason_codes: tuple[str, ...]


class RecommendationResult(FrozenSchema):
    """추천 묶음. ranked는 하나의 ranking_mode만 사용하며 두 모드의 시간을 섞지 않는다."""

    ranking_mode: RankingMode
    mode_reason_codes: tuple[str, ...]
    recommended_timed_candidate_id: str | None
    ranked: tuple[RankedCandidate, ...]
    excluded: tuple[ExcludedCandidate, ...]
    # 잘린 탐색·평가에서는 전체 최적을 확정하지 않는다.
    coverage_complete: bool
    reason_codes: tuple[str, ...]
