"""균형 시뮬레이션(오프라인, DB·HTTP 없음): 한 검색의 후보들 사이에서 이용자 선택과 혼잡이 서로 맞물린 뒤의 분포.

같은 출발·도착을 한 시간에 N명이 검색한다고 두고, 후보를 고른 사람이 그 후보의 버스 구간 재차에 더해지게 한다.

    구간 재차(1대당) = 집계 기준 재차 + (그 구간을 지나는 후보들의 선택 인원 합) ÷ 시간당 통과 횟수
    후보 비용        = 이동시간 + Σ 버스 구간 탑승시간 × (혼잡 단계 가중치 − 1)      검색 순위와 같은 식
    선택 비율(로짓)  = exp(−θ × 비용) ÷ Σ exp(−θ × 비용)
    연속평균법       = x[n+1] = x[n] + (y[n] − x[n]) ÷ n,   y[n] = N × 선택 비율(x[n]일 때의 비용)

- 균형 분포는 x와 y가 같아지는 점(확률적 이용자 균형)이다. 누구도 혼자 선택을 바꿔 체감 비용을 줄일 수 없는 배분이다.
- 혼잡 가중치는 단계가 바뀔 때만 변하는 계단 함수라 x와 y가 정확히 같아지지 않을 수 있다. 그때는 평균 분포가 단계 경계
  근처에 머문다. 수렴 여부(converged)와 남은 차이(gap)를 그대로 돌려준다.
- N과 θ는 근거 있는 값이 없어 실행할 때 준다. 앱 사용자는 전체 승객의 일부이므로 결과는 실제 혼잡 분산 효과가 아니라
  추천 방식의 성질을 보는 용도다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.modules.congestion.levels import LEVELS, LevelPolicy, level_of


@dataclass(frozen=True)
class Link:
    """버스 구간이 지나는 경유 순번 하나. base_load는 집계 기준 1대당 재차(모르면 0으로 두고 assumed를 남긴다)."""

    seq: int
    base_load: float
    trips_per_hour: float


@dataclass(frozen=True)
class Ride:
    """후보의 버스 구간 하나. links가 비어 있으면 혼잡을 계산하지 못하는 구간이며 비용을 더하지 않는다."""

    route_key: tuple[Any, str]          # (노선 내부 ID, 패턴)
    ride_seconds: float
    links: tuple[Link, ...] = ()
    assumed: tuple[str, ...] = ()       # 가정으로 채운 값(예: base_load_unknown, trips_from_timetable)


@dataclass(frozen=True)
class Option:
    option_id: str
    time_seconds: float
    rides: tuple[Ride, ...] = ()
    label: str | None = None


@dataclass(frozen=True)
class OptionResult:
    option_id: str
    label: str | None
    time_seconds: float
    cost_before: float                  # 추가 인원이 없을 때(지금 추천 기준)의 비용
    cost_at_equilibrium: float
    share_all_to_best: float            # 모두 비용이 가장 낮은 후보를 고를 때
    share_before: float                 # 지금 비용으로 로짓 선택(혼잡 되먹임 없음)
    share_at_equilibrium: float
    levels_before: tuple[str | None, ...]
    levels_at_equilibrium: tuple[str | None, ...]
    assumed: tuple[str, ...] = ()


@dataclass(frozen=True)
class Result:
    demand_per_hour: float
    theta: float
    iterations: int
    converged: bool
    gap: float                          # 마지막 반복의 max|y − x| ÷ N
    options: tuple[OptionResult, ...] = field(default_factory=tuple)

    def max_share(self, name: str) -> float:
        return max((getattr(option, name) for option in self.options), default=0.0)


def link_flows(options: Sequence[Option], flows: Sequence[float]) -> dict[tuple[Any, str, int], float]:
    """(노선, 패턴, 경유 순번) → 그 구간을 지나는 후보들의 선택 인원 합(명/시간)."""
    totals: dict[tuple[Any, str, int], float] = {}
    for option, flow in zip(options, flows):
        seen: set[tuple[Any, str, int]] = set()
        for ride in option.rides:
            for link in ride.links:
                key = (*ride.route_key, link.seq)
                if key not in seen:             # 한 후보가 같은 구간을 두 번 세지 않는다
                    seen.add(key)
                    totals[key] = totals.get(key, 0.0) + flow
    return totals


def ride_level(ride: Ride, added: dict[tuple[Any, str, int], float], policy: LevelPolicy) -> int | None:
    """타는 구간 중 가장 붐비는 구간의 단계 번호(1~5). 계산하지 못하면 None."""
    if not ride.links:
        return None
    load = max(link.base_load + added.get((*ride.route_key, link.seq), 0.0) / link.trips_per_hour
               for link in ride.links)
    return level_of(load, policy)


def option_costs(options: Sequence[Option], flows: Sequence[float], policy: LevelPolicy
                 ) -> tuple[list[float], list[tuple[str | None, ...]]]:
    """선택 인원이 flows일 때 후보별 비용(초)과 버스 구간별 단계."""
    added = link_flows(options, flows)
    costs, names = [], []
    for option in options:
        cost, row = option.time_seconds, []
        for ride in option.rides:
            index = ride_level(ride, added, policy)
            row.append(None if index is None else LEVELS[index - 1])
            if index is not None:
                cost += ride.ride_seconds * (policy.cost_weights[index - 1] - 1.0)
        costs.append(cost)
        names.append(tuple(row))
    return costs, names


def logit_shares(costs: Sequence[float], theta: float) -> list[float]:
    """비용이 낮을수록 많이 고르는 로짓 선택 비율. θ가 클수록 가장 싼 후보로 몰린다."""
    lowest = min(costs)
    weights = [math.exp(-theta * (cost - lowest)) for cost in costs]
    total = sum(weights)
    return [weight / total for weight in weights]


def simulate(options: Sequence[Option], policy: LevelPolicy, *, demand_per_hour: float, theta: float,
             max_iterations: int, tolerance: float) -> Result:
    """연속평균법으로 균형 분포를 구하고, 지금 분포(모두 1위 선택 / 되먹임 없는 로짓)와 함께 돌려준다."""
    if not options:
        return Result(demand_per_hour, theta, 0, True, 0.0)
    if len(policy.cost_weights) != len(LEVELS):
        raise ValueError("혼잡 단계별 가중치(cost_weights)가 있어야 균형을 계산할 수 있습니다.")
    if demand_per_hour <= 0 or theta <= 0 or max_iterations < 1 or tolerance <= 0:
        raise ValueError("demand_per_hour, theta, max_iterations, tolerance는 0보다 커야 합니다.")
    count = len(options)
    cost_before, levels_before = option_costs(options, [0.0] * count, policy)
    share_before = logit_shares(cost_before, theta)
    best = min(range(count), key=lambda i: (cost_before[i], i))
    flows = [demand_per_hour * share for share in share_before]
    gap, converged, iterations = math.inf, False, 0
    for step in range(1, max_iterations + 1):
        iterations = step
        costs, _ = option_costs(options, flows, policy)
        target = [demand_per_hour * share for share in logit_shares(costs, theta)]
        gap = max(abs(t - x) for t, x in zip(target, flows)) / demand_per_hour
        if gap < tolerance:
            converged = True
            break
        flows = [x + (t - x) / step for x, t in zip(flows, target)]
    cost_final, levels_final = option_costs(options, flows, policy)
    return Result(demand_per_hour, theta, iterations, converged, gap, tuple(
        OptionResult(
            option_id=option.option_id, label=option.label, time_seconds=option.time_seconds,
            cost_before=cost_before[i], cost_at_equilibrium=cost_final[i],
            share_all_to_best=1.0 if i == best else 0.0, share_before=share_before[i],
            share_at_equilibrium=flows[i] / demand_per_hour,
            levels_before=levels_before[i], levels_at_equilibrium=levels_final[i],
            assumed=tuple(sorted({note for ride in option.rides for note in ride.assumed})))
        for i, option in enumerate(options)))
