"""Phase 5의 현재 경로와 대안 비교 규칙."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

RankingMode = Literal["nominal", "expected"]


@dataclass(frozen=True)
class RouteOption:
    candidate_id: str
    ranking_mode: RankingMode
    cost_seconds: float | None
    comparison_key: str
    startable: bool
    coverage_complete: bool
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChangeDecision:
    status: Literal["propose", "keep", "data_limited"]
    current_candidate_id: str
    proposed_candidate_id: str | None
    improvement_seconds: float | None
    reason_codes: tuple[str, ...]
    requires_user_confirmation: bool = False


def _valid(option: RouteOption) -> bool:
    value = option.cost_seconds
    return (option.startable and option.coverage_complete and bool(option.comparison_key)
            and value is not None and math.isfinite(value) and value >= 0)


def decide_change(current: RouteOption, alternatives: list[RouteOption] | tuple[RouteOption, ...], *,
                  min_improvement_seconds: float) -> ChangeDecision:
    """동일 평가 모드·비교 시점의 실행 가능한 경로 중 임계값을 넘게 개선된 경우만 제안한다."""
    if not math.isfinite(min_improvement_seconds) or min_improvement_seconds < 0:
        raise ValueError("min_improvement_seconds는 유한한 0 이상 값이어야 합니다.")
    if not _valid(current):
        return ChangeDecision("data_limited", current.candidate_id, None, None,
                              ("current_route_not_comparable",))
    eligible = [item for item in alternatives if item.candidate_id != current.candidate_id and _valid(item)
                and item.ranking_mode == current.ranking_mode and item.comparison_key == current.comparison_key]
    if not eligible:
        return ChangeDecision("data_limited", current.candidate_id, None, None,
                              ("no_comparable_alternative",))
    best = min(eligible, key=lambda item: (item.cost_seconds, item.candidate_id))
    improvement = current.cost_seconds - best.cost_seconds
    if improvement <= 0:
        return ChangeDecision("keep", current.candidate_id, None, improvement, ("current_route_not_slower",))
    if improvement <= min_improvement_seconds:
        return ChangeDecision("keep", current.candidate_id, None, improvement,
                              ("improvement_below_or_equal_threshold",))
    return ChangeDecision("propose", current.candidate_id, best.candidate_id, improvement,
                          ("alternative_has_lower_comparable_cost",), True)
