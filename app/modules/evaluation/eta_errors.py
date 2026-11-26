"""단일 차량 ETA 오차 표본(app.eta_error_samples)의 생성 규칙(순수 함수, DB 접근 없음).

오차 = 실제 도착 − 예측 도착(초). 도착 이벤트 하나(차량 한 대가 정류소 하나에 도착한 사건)에 같은 차량번호·정류소·노선의
이전 도착 예측을 잇는다.
- '다음 차량 출발 전' 항목(대전시 MSG_TP=07)은 예측이 아니므로 잇지 않는다.
- 예측 기준 시각이 도착(상한)보다 늦거나, 기준 시각부터 도착(상한)까지가 설정한 창을 넘으면 잇지 않는다(같은 차량의 다른
  운행을 잇지 않게 한다).
- 잔여시간(예측 도착 − 기준 시각)을 설정한 경계로 구간화하고, 한 운행에서 구간마다 가장 이른 예측 하나만 표본으로 만든다.
  같은 운행의 반복 예측을 독립 표본으로 세지 않는다(테이블 고유 키: 운행·생성 방식·모델 버전·잔여시간 구간).
- 도착을 범위로만 알면 오차도 하한·상한으로 남기고 대표값으로 바꾸지 않는다.
- 생성 방식(eta_source)·모델 버전·기준 시각 출처를 표본에 남겨, 다른 방식의 오차를 한 분포로 섞지 않게 한다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.modules.evaluation.schemas import EtaErrorSample

RULE_VERSION = "eta-error-sample/1"


def horizon_bucket(horizon_seconds: float, edges_seconds: Sequence[float]) -> str:
    """잔여시간 구간 이름. 경계가 120, 300, 600이면 0-120, 120-300, 300-600, 600+ 가운데 하나다(아래 경계 포함)."""
    lower = 0.0
    for edge in edges_seconds:
        if horizon_seconds < edge:
            return f"{lower:g}-{edge:g}"
        lower = edge
    return f"{lower:g}+"


@dataclass(frozen=True)
class ArrivalPrediction:
    """저장된 도착 예측 1건(transit_observations kind=arrival)."""

    observation_id: int
    basis_at: datetime
    predicted_arrival_at: datetime
    basis_source: str | None
    eta_source: str = "provider"
    model_version: str | None = None
    # 다음 차량이 아직 출발하지 않았다는 항목(대전시 MSG_TP=07). 잔여시간이 실제 예측이 아니다.
    waiting_departure: bool = False


@dataclass(frozen=True)
class ObservedArrival:
    """도출한 도착 이벤트 1건(transit_observations kind=event). 점 시각이면 lower_at = upper_at이다."""

    observation_id: int
    run_group: str
    quality: str
    lower_at: datetime
    upper_at: datetime


@dataclass(frozen=True)
class ErrorSampleRecord:
    run_group: str
    eta_source: str
    model_version: str | None
    basis_source: str | None
    horizon_bucket: str
    horizon_seconds: float
    error_lower_seconds: float
    error_upper_seconds: float
    quality: str
    basis_at: datetime
    predicted_arrival_at: datetime
    arrival_lower_at: datetime
    arrival_upper_at: datetime
    prediction_observation_id: int
    event_observation_id: int


def build_error_samples(arrival: ObservedArrival, predictions: Iterable[ArrivalPrediction], *,
                        horizon_edges_seconds: Sequence[float], match_window_seconds: float) -> list[ErrorSampleRecord]:
    """도착 이벤트 하나의 오차 표본. 잔여시간 구간마다 가장 이른 예측 하나만 쓴다."""
    chosen: dict[tuple[str, str | None, str], ArrivalPrediction] = {}
    for prediction in predictions:
        if prediction.waiting_departure:
            continue
        lead_seconds = (arrival.upper_at - prediction.basis_at).total_seconds()
        if lead_seconds < 0 or lead_seconds > match_window_seconds:
            continue
        horizon = (prediction.predicted_arrival_at - prediction.basis_at).total_seconds()
        if horizon < 0:
            continue
        key = (prediction.eta_source, prediction.model_version, horizon_bucket(horizon, horizon_edges_seconds))
        current = chosen.get(key)
        if current is None or (prediction.basis_at, prediction.observation_id) < (current.basis_at,
                                                                                  current.observation_id):
            chosen[key] = prediction
    records = []
    for (eta_source, model_version, bucket), prediction in sorted(chosen.items(), key=lambda item: item[1].basis_at):
        records.append(ErrorSampleRecord(
            run_group=arrival.run_group, eta_source=eta_source, model_version=model_version,
            basis_source=prediction.basis_source, horizon_bucket=bucket,
            horizon_seconds=(prediction.predicted_arrival_at - prediction.basis_at).total_seconds(),
            error_lower_seconds=(arrival.lower_at - prediction.predicted_arrival_at).total_seconds(),
            error_upper_seconds=(arrival.upper_at - prediction.predicted_arrival_at).total_seconds(),
            quality="exact" if arrival.quality == "exact" else "bounded",
            basis_at=prediction.basis_at, predicted_arrival_at=prediction.predicted_arrival_at,
            arrival_lower_at=arrival.lower_at, arrival_upper_at=arrival.upper_at,
            prediction_observation_id=prediction.observation_id, event_observation_id=arrival.observation_id))
    return records


QUANTILES = (0.1, 0.25, 0.5, 0.75, 0.9)


def quantile(sorted_values: Sequence[float], q: float) -> float | None:
    """정렬된 값의 분위수(선형 보간). 값이 없으면 None이다."""
    if not sorted_values:
        return None
    position = (len(sorted_values) - 1) * q
    below = int(position)
    above = min(below + 1, len(sorted_values) - 1)
    return sorted_values[below] + (sorted_values[above] - sorted_values[below]) * (position - below)


def _distribution(values: Iterable[float]) -> dict[str, float] | None:
    ordered = sorted(values)
    if not ordered:
        return None
    return {("median" if q == 0.5 else f"p{round(q * 100)}"): round(quantile(ordered, q), 1) for q in QUANTILES}


def summarize_samples(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """표본 현황: 생성 방식·모델 버전·기준 시각 출처·잔여시간 구간별 표본 수(독립 운행 기준)와 오차 분포.

    점 시각 표본의 오차 분포(exact)와, 전체 표본의 오차 하한 분포(lower)·상한 분포(upper)를 따로 낸다. 범위 표본을 중간값
    같은 대표값으로 바꿔 한 분포로 합치지 않는다.
    """
    groups: dict[tuple, list[Mapping[str, Any]]] = {}
    for row in rows:
        key = (row["eta_source"], row["model_version"], row["basis_source"], row["horizon_bucket"])
        groups.setdefault(key, []).append(row)

    def bucket_order(name: str) -> float:
        try:
            return float(name.rstrip("+").split("-")[0])
        except ValueError:
            return float("inf")

    summary = []
    for key in sorted(groups, key=lambda k: (k[0], k[1] or "", k[2] or "", bucket_order(k[3]), k[3])):
        items = groups[key]
        exact = [row for row in items if row["quality"] == "exact"]
        summary.append({
            "eta_source": key[0], "model_version": key[1], "basis_source": key[2], "horizon_bucket": key[3],
            "samples": len(items), "independent_runs": len({row["run_group"] for row in items}),
            "by_quality": {"exact": len(exact), "bounded": len(items) - len(exact)},
            "error_seconds": {
                "exact": _distribution(row["error_lower_seconds"] for row in exact),
                "lower": _distribution(row["error_lower_seconds"] for row in items),
                "upper": _distribution(row["error_upper_seconds"] for row in items),
            },
            "bounded_width_seconds": _distribution(row["error_upper_seconds"] - row["error_lower_seconds"]
                                                   for row in items if row["quality"] != "exact"),
            "latest_arrival_at": max(row["arrival_upper_at"] for row in items),
            "latest_created_at": max(row["created_at"] for row in items),
        })
    return summary


def error_samples(rows: Iterable[Mapping[str, Any]]) -> list[EtaErrorSample]:
    """DB 행을 평가 입력으로 바꾼다."""
    return [EtaErrorSample(
        sample_id=str(row["id"]), run_group_key=row["run_group"], eta_source=row["eta_source"],
        model_version=row["model_version"], basis_source=row["basis_source"], horizon_bucket=row["horizon_bucket"],
        error_lower_seconds=row["error_lower_seconds"], error_upper_seconds=row["error_upper_seconds"],
    ) for row in rows]
