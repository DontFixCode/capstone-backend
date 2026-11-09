"""Phase 5: 이동 중 기존 경로와 대안 경로의 변경 *제안* 판단.

DB 접근/경로 탐색은 하지 않는 순수 함수다. 자료가 부족하면 변경을 제안하지 않으며,
실제 선택 경로의 변경은 사용자 승인과 별도 트랜잭션에서만 수행해야 한다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

RankingMode = Literal["nominal", "expected"]
DecisionStatus = Literal["propose", "keep", "data_limited"]


@dataclass(frozen=True)
class RouteOption:
    candidate_id: str
    ranking_mode: RankingMode
    cost_seconds: float | None
    # 동일한 출발 기준시각·목적지·관측 버전 등 비교 조건에 대한 상위 계층의 식별자.
    comparison_key: str
    startable: bool
    coverage_complete: bool
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChangeDecision:
    status: DecisionStatus
    current_candidate_id: str
    proposed_candidate_id: str | None
    improvement_seconds: float | None
    reason_codes: tuple[str, ...]
    requires_user_confirmation: bool = False


def _valid(option: RouteOption) -> bool:
    value = option.cost_seconds
    return (
        option.startable
        and option.coverage_complete
        and bool(option.comparison_key)
        and value is not None
        and math.isfinite(value)
        and value >= 0
    )


def decide_change(
    current: RouteOption,
    alternatives: list[RouteOption] | tuple[RouteOption, ...],
    *,
    min_improvement_seconds: float,
) -> ChangeDecision:
    """동일 조건에서 엄격히 더 빠른 대안만 제안한다.

    min_improvement_seconds는 프로젝트 설정의 검증된 값을 호출부가 제공해야 하며,
    미정 설정을 임의의 기본값으로 대체하지 않는다. 동률/임계값 딱 일치는 유지한다.
    """
    if not math.isfinite(min_improvement_seconds) or min_improvement_seconds < 0:
        raise ValueError("min_improvement_seconds는 유한한 0 이상 값이어야 합니다.")

    if not _valid(current):
        return ChangeDecision("data_limited", current.candidate_id, None, None,
                              ("current_route_not_comparable",))

    eligible = [
        option for option in alternatives
        if option.candidate_id != current.candidate_id
        and _valid(option)
        and option.ranking_mode == current.ranking_mode
        and option.comparison_key == current.comparison_key
    ]
    if not eligible:
        return ChangeDecision("data_limited", current.candidate_id, None, None,
                              ("no_comparable_alternative",))

    # 입력 순서가 바뀌어도 결과가 같도록 후보 ID로 동률을 결정한다.
    best = min(eligible, key=lambda option: (option.cost_seconds, option.candidate_id))
    improvement = current.cost_seconds - best.cost_seconds
    if improvement <= 0:
        return ChangeDecision("keep", current.candidate_id, None, improvement,
                              ("current_route_not_slower",))
    if improvement <= min_improvement_seconds:
        return ChangeDecision("keep", current.candidate_id, None, improvement,
                              ("improvement_below_or_equal_threshold",))
    return ChangeDecision("propose", current.candidate_id, best.candidate_id, improvement,
                          ("alternative_has_lower_comparable_cost",), True)
