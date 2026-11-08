"""추천 모드 선택·비용 계산·정렬·추천 이유 생성을 담당한다. 추천 유지·변경 판단은 이동 관리 구현 때 추가한다.

초기 모드는 nominal이고 사용자 맞춤형·내시균형 bias는 0이다. 비교 대상 전체를 같은 조건으로 평가할 수 있을 때만
expected를 적용하며, 한 순위에서 두 모드의 시간을 섞지 않는다. 비용에 확률을 넣지 않으므로 성공 확률과 추천 점수는
별개의 값이다.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.modules.evaluation.schemas import EvaluatedCandidate, EvaluationResult
from app.modules.recommendation.schemas import (
    ExcludedCandidate,
    RankedCandidate,
    RankingMode,
    RecommendationResult,
)


def recommendation_cost(
    time_seconds: float,
    *,
    user_bias_seconds: float = 0.0,
    equilibrium_bias_seconds: float = 0.0,
) -> float:
    """추천 비용 = 선택한 모드의 이동시간 + 사용자 맞춤형 bias + 내시균형 bias. 실패 손실은 다시 더하지 않는다."""
    return time_seconds + user_bias_seconds + equilibrium_bias_seconds


def select_ranking_mode(
    candidates: Sequence[EvaluatedCandidate],
    limited: Sequence[EvaluatedCandidate],
) -> tuple[RankingMode, tuple[str, ...]]:
    """비교 묶음의 추천 모드와 이유를 정한다.

    기준 ETA 후보 중 하나라도 기대시간이 없으면 그 후보만 빼지 않고 묶음 전체를 nominal로 처리한다. 음수 기준 여유
    후보는 기대시간까지 평가된 경우에만 expected 비교 대상이 된다.
    """
    nominal_group = [item for item in candidates if _nominal_eligible(item)]
    negative_group = [item for item in limited if _negative_slack_with_expected(item)]
    group = [*nominal_group, *negative_group]
    if not group:
        return "nominal", ("no_comparable_candidates",)
    if any(item.expected_total_seconds is None for item in nominal_group):
        return "nominal", ("expected_time_missing_in_group",)
    if len({item.expected_comparison_key for item in group}) != 1:
        return "nominal", ("expected_conditions_not_comparable",)
    return "expected", ("expected_time_available_for_group",)


def recommend_candidates(evaluation: EvaluationResult) -> RecommendationResult:
    """평가 결과를 한 모드로 정렬해 추천 묶음을 만든다.

    nominal·expected 모두 비용 → 환승 횟수 → 도보시간 → 안정적인 후보 ID 순이다. 순위에 들지 못한 후보는 숫자를
    채우지 않고 이유와 함께 제외 목록으로 반환한다.
    """
    mode, mode_reasons = select_ranking_mode(evaluation.candidates, evaluation.limited)
    rankable: list[EvaluatedCandidate] = []
    excluded: list[ExcludedCandidate] = []
    for item in (*evaluation.candidates, *evaluation.limited):
        if _time_for_mode(item, mode) is not None:
            rankable.append(item)
        else:
            excluded.append(_excluded(item, mode))

    ordered = sorted(rankable, key=lambda item: _sort_key(item, mode))
    ranked = tuple(
        _ranked(item, rank, mode, ordered[rank] if rank < len(ordered) else None)
        for rank, item in enumerate(ordered, start=1)
    )

    coverage_reasons: list[str] = []
    if evaluation.search_coverage.truncated:
        coverage_reasons.append("search_truncated")
    if evaluation.coverage.truncated:
        coverage_reasons.append("evaluation_truncated")
    reasons = list(coverage_reasons)
    if not ranked:
        reasons.append("no_recommendable_candidate")

    return RecommendationResult(
        ranking_mode=mode,
        mode_reason_codes=mode_reasons,
        recommended_timed_candidate_id=ranked[0].timed_candidate_id if ranked else None,
        ranked=ranked,
        excluded=tuple(sorted(excluded, key=lambda item: item.timed_candidate_id)),
        coverage_complete=not coverage_reasons,
        reason_codes=tuple(reasons),
    )


def _nominal_eligible(item: EvaluatedCandidate) -> bool:
    return (
        item.time_status == "available"
        and item.nominally_feasible is True
        and item.nominal_total_seconds is not None
    )


def _negative_slack_with_expected(item: EvaluatedCandidate) -> bool:
    return (
        item.time_status == "available"
        and item.connection_status == "negative_slack"
        and item.expected_total_seconds is not None
    )


def _time_for_mode(item: EvaluatedCandidate, mode: RankingMode) -> float | None:
    if mode == "nominal":
        return item.nominal_total_seconds if _nominal_eligible(item) else None
    if _nominal_eligible(item) or _negative_slack_with_expected(item):
        return item.expected_total_seconds
    return None


def _sort_key(item: EvaluatedCandidate, mode: RankingMode) -> tuple:
    cost = recommendation_cost(_time_for_mode(item, mode))
    return cost, item.transfer_count, item.walk_seconds, item.timed_candidate_id


def _ranked(
    item: EvaluatedCandidate,
    rank: int,
    mode: RankingMode,
    following: EvaluatedCandidate | None,
) -> RankedCandidate:
    time_seconds = _time_for_mode(item, mode)
    reasons = [f"ranked_by_{mode}_cost"]
    if rank == 1:
        reasons.append("recommended_lowest_cost")
        if following is not None:
            reasons.extend(_tie_break_reasons(item, following, mode))
    if item.connection_status == "negative_slack":
        reasons.append("nominal_connection_negative_slack")
    if item.journey_probability is None:
        reasons.append("journey_probability_unavailable")
    if any(value is None for value in item.transfer_probabilities):
        reasons.append("transfer_probability_unavailable")
    return RankedCandidate(
        rank=rank,
        static_candidate_id=item.static_candidate_id,
        timed_candidate_id=item.timed_candidate_id,
        ranking_mode=mode,
        time_seconds=time_seconds,
        user_bias_seconds=0.0,
        equilibrium_bias_seconds=0.0,
        cost_seconds=recommendation_cost(time_seconds),
        transfer_count=item.transfer_count,
        walk_seconds=item.walk_seconds,
        first_boarding_probability=item.first_boarding_probability,
        transfer_probabilities=item.transfer_probabilities,
        journey_probability=item.journey_probability,
        reason_codes=tuple(reasons),
    )


def _tie_break_reasons(item: EvaluatedCandidate, following: EvaluatedCandidate, mode: RankingMode) -> list[str]:
    """1위와 2위의 비용이 같을 때 어떤 기준으로 갈렸는지 반환한다."""
    if _time_for_mode(item, mode) != _time_for_mode(following, mode):
        return []
    if item.transfer_count != following.transfer_count:
        return ["tie_broken_by_transfer_count"]
    if item.walk_seconds != following.walk_seconds:
        return ["tie_broken_by_walk_seconds"]
    return ["tie_broken_by_candidate_id"]


def _excluded(item: EvaluatedCandidate, mode: RankingMode) -> ExcludedCandidate:
    reasons = set(item.reason_codes)
    if item.connection_status == "negative_slack":
        reasons.add("nominal_connection_not_established")
    elif item.connection_status == "confirmed_infeasible":
        reasons.add("confirmed_infeasible")
    elif item.time_status != "available":
        reasons.add("time_incomplete")
    elif mode == "expected":
        reasons.add("expected_time_unavailable")
    return ExcludedCandidate(
        static_candidate_id=item.static_candidate_id,
        timed_candidate_id=item.timed_candidate_id,
        time_status=item.time_status,
        nominally_feasible=item.nominally_feasible,
        connection_status=item.connection_status,
        limited_at_ride_index=item.limited_at_ride_index,
        reason_codes=tuple(sorted(reasons)),
    )
