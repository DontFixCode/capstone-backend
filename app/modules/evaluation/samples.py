"""환승 표본(transfer_samples)·공동 관측 묶음(journey_scenes)의 생성 기록·평가 입력 변환·사후 판정 규칙.

DB 접근은 app/modules/evaluation/repository.py가 맡고 여기서는 순수 함수만 둔다.
- 예측 당시 값은 불변이다. 같은 조건(입력 수집 근거·예측 쌍·경유 위치·차량/회차·도보·준비·모델·정책)의 요청은
  같은 sample_key/scene_key가 되어 표본을 재사용한다. 평가 시각·사용자·후보 ID는 키에 넣지 않는다.
- 운행 회차(run_key)가 확인되지 않은 예측은 사후 관측을 같은 운행에 연결할 수 없으므로 표본·묶음을 만들지 않는다.
- 사후 판정은 같은 패턴·경유 위치·회차의 '도착' 점 이벤트(quality=exact)만 쓴다. 범위만 확인된 관측은 범위로
  보존하고 점 오차로 바꾸지 않는다. 판정 기한이 지나도 점 시각이 없으면 insufficient로 둔다.
- 사용자 식별자·개인 출발지·목적지·도보 경로는 공통 표본에 넣지 않는다(정류소 간 환승 도보시간만 고정값으로 보존).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.modules.evaluation.schemas import (
    EvaluatedCandidate,
    EvaluationPolicy,
    JointScene,
    ObservedEvent,
    PredictionOutcome,
    SceneRide,
    TimedLeg,
    TransferErrorSample,
    VehicleEta,
)


def _digest(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def condition_key(provider: str, policy: EvaluationPolicy, eta_basis_policy: str | None = None) -> str:
    """비교 가능한 표본·묶음·구간 관측의 조건 키. MVP는 세분화 없는 공통 표본 모델(제공기관·정책 버전)이다.

    ETA 기준 시각을 근사 정책으로 만든 예측은 생성 방식이 다르므로 다른 조건으로 둔다(서로 다른 방식의 잔차를 같은 분포로
    합치지 않는다, Phase 3 - Algorithms 2.1 ④). 기본 정책(unconfirmed)의 키는 기존과 같다.
    """
    key = f"{provider}:common:{policy.sample_policy_version}:{policy.timing_policy_version}"
    if eta_basis_policy and eta_basis_policy != "unconfirmed":
        key += f":eta-basis-{eta_basis_policy}"
    return key


def eta_context(eta: VehicleEta, *, route_id: str) -> dict[str, Any]:
    return {
        "route_id": route_id,
        "pattern_id": eta.pattern_id,
        "stop_id": eta.stop_id,
        "stop_order": eta.stop_order,
        "vehicle_id": eta.vehicle_id,
        "run_key": eta.run_key,
        "predicted_arrival_at": eta.predicted_arrival_at.isoformat(),
        "eta_source": eta.eta_source,
        "model_version": eta.model_version,
        "horizon_seconds": eta.horizon_seconds,
        "basis_at": eta.basis_at.isoformat(),
        "basis_source": eta.basis_source,
        "source_generated_at": eta.source_generated_at.isoformat() if eta.source_generated_at else None,
        "collected_at": eta.collected_at.isoformat(),
        "source_ref": eta.source_ref,
        "segment_sample_counts": list(eta.segment_sample_counts),
    }


@dataclass
class SampleRecord:
    sample_key: str
    cohort_key: str
    independence_group: str
    evaluated_at: datetime
    front_prediction_at: datetime
    next_prediction_at: datetime
    front_stop_external_id: str
    next_stop_external_id: str
    walk_seconds: float
    buffer_seconds: float
    prediction_context: dict[str, Any]
    predicted_probability: float | None
    probability_context: dict[str, Any]
    label_deadline_at: datetime


@dataclass
class SampleSkip:
    timed_candidate_id: str
    transfer_index: int
    reason: str


def build_sample_records(
    candidate: EvaluatedCandidate,
    *,
    static_version: str,
    cohort: str,
    policy: EvaluationPolicy,
    evaluated_at: datetime,
    label_deadline_seconds: float,
    config_version: str,
) -> tuple[list[tuple[int, SampleRecord]], list[SampleSkip]]:
    """후보의 환승마다 표본 기록을 만든다. (환승 순번, 기록) 목록과 만들지 못한 이유를 반환한다."""
    records: list[tuple[int, SampleRecord]] = []
    skips: list[SampleSkip] = []
    for index in range(1, candidate.transfer_count + 1):
        def skip(reason: str) -> None:
            skips.append(SampleSkip(candidate.timed_candidate_id, index, reason))

        if len(candidate.legs) <= index or len(candidate.connections) <= index:
            skip("transfer_not_reached")
            continue
        front_leg, next_leg = candidate.legs[index - 1], candidate.legs[index]
        connection = candidate.connections[index]
        front, following = front_leg.alight_eta, next_leg.board_eta
        if front is None:
            skip("front_alight_eta_unavailable")
            continue
        if connection.walk_seconds is None:
            skip("transfer_walk_unavailable")
            continue
        if front.run_key is None or following.run_key is None:
            skip("run_unconfirmed")
            continue
        front_ctx = eta_context(front, route_id=front_leg.route_id)
        next_ctx = eta_context(following, route_id=next_leg.route_id)
        key_payload = {
            "static_version": static_version,
            "front": {k: front_ctx[k] for k in ("pattern_id", "stop_id", "stop_order", "vehicle_id", "run_key",
                                                "predicted_arrival_at", "eta_source", "model_version", "basis_at",
                                                "source_ref")},
            "next": {k: next_ctx[k] for k in ("pattern_id", "stop_id", "stop_order", "vehicle_id", "run_key",
                                              "predicted_arrival_at", "eta_source", "model_version", "basis_at",
                                              "source_ref")},
            "walk_seconds": connection.walk_seconds,
            "buffer_seconds": connection.buffer_seconds,
            "timing_policy_version": policy.timing_policy_version,
            "sample_policy_version": policy.sample_policy_version,
            "segment_model_version": policy.segment_model_version,
        }
        probability = candidate.transfer_probabilities[index - 1]
        counts = candidate.provenance.transfer_sample_counts
        rejections = candidate.provenance.transfer_sample_rejections
        records.append((index, SampleRecord(
            sample_key=_digest(key_payload),
            cohort_key=cohort,
            independence_group=f"{front.run_key}>{following.run_key}",
            evaluated_at=max(front.basis_at, following.basis_at),
            front_prediction_at=front.predicted_arrival_at,
            next_prediction_at=following.predicted_arrival_at,
            front_stop_external_id=front.stop_id,
            next_stop_external_id=following.stop_id,
            walk_seconds=connection.walk_seconds,
            buffer_seconds=connection.buffer_seconds,
            prediction_context={
                "front": front_ctx,
                "next": next_ctx,
                "static_version": static_version,
                "timing_policy_version": policy.timing_policy_version,
                "sample_policy_version": policy.sample_policy_version,
                "segment_model_version": policy.segment_model_version,
                "config_version": config_version,
                "slack_seconds": connection.slack_seconds,
            },
            predicted_probability=probability,
            probability_context={
                "sample_count": counts[index - 1] if len(counts) >= index else 0,
                "rejections": rejections[index - 1] if len(rejections) >= index else {},
                "min_probability_samples": policy.min_probability_samples,
                "sample_policy_version": policy.sample_policy_version,
                "cohort_key": cohort,
                "reason": None if probability is not None else "transfer_probability_unavailable",
            },
            label_deadline_at=evaluated_at + timedelta(seconds=label_deadline_seconds),
        )))
    return records, skips


@dataclass
class SceneRecord:
    scene_key: str
    cohort_key: str
    independence_group: str
    information_at: datetime
    static_version: str
    route_context: dict[str, Any]
    predictions: list[dict[str, Any]]
    label_deadline_at: datetime


def build_scene_record(
    static_candidate_id: str,
    timed: Sequence[EvaluatedCandidate],
    *,
    static_version: str,
    cohort: str,
    policy: EvaluationPolicy,
    evaluated_at: datetime,
    label_deadline_seconds: float,
) -> tuple[SceneRecord | None, str | None]:
    """정적 경로 하나의 공동 예측 묶음. 계획 차량과 실패 대응에 쓸 다음 차량(vehicle_rank)의 승하차 예측을 함께 고정한다.

    모든 구간의 승차·하차 예측과 운행 회차가 확인된 차량 조합이 하나 이상 있어야 만든다.
    """
    complete = [c for c in timed if c.static_candidate_id == static_candidate_id
                and c.legs and all(leg.alight_eta is not None for leg in c.legs)
                and len(c.legs) == c.transfer_count + 1]
    if not complete:
        return None, "complete_predictions_unavailable"
    legs: dict[tuple[int, int], TimedLeg] = {}
    for candidate in complete:
        for ride_index, leg in enumerate(candidate.legs):
            legs.setdefault((ride_index, leg.vehicle_rank), leg)
    if any(leg.run_key is None or leg.alight_eta.run_key is None for leg in legs.values()):
        return None, "run_unconfirmed"
    predictions = []
    for (ride_index, rank), leg in sorted(legs.items()):
        predictions.append({
            "ride_index": ride_index,
            "pattern_id": leg.pattern_id,
            "board_position": leg.board_position,
            "alight_position": leg.alight_position,
            "vehicle_rank": rank,
            "vehicle_id": leg.vehicle_id,
            "run_key": leg.run_key,
            "board": eta_context(leg.board_eta, route_id=leg.route_id),
            "alight": eta_context(leg.alight_eta, route_id=leg.route_id),
        })
    first = complete[0]
    rides = [{"ride_index": i, "route_id": leg.route_id, "pattern_id": leg.pattern_id,
              "board_stop_id": leg.board_stop_id, "board_position": leg.board_position,
              "alight_stop_id": leg.alight_stop_id, "alight_position": leg.alight_position}
             for i, leg in enumerate(first.legs)]
    key_payload = {
        "static_version": static_version,
        "rides": rides,
        "predictions": [{k: p[k] for k in ("ride_index", "vehicle_rank", "run_key")}
                        | {"board": p["board"]["predicted_arrival_at"], "alight": p["alight"]["predicted_arrival_at"],
                           "basis": p["board"]["basis_at"], "source": p["board"]["eta_source"]}
                        for p in predictions],
        "timing_policy_version": policy.timing_policy_version,
        "sample_policy_version": policy.sample_policy_version,
    }
    information_at = max(max(leg.board_eta.basis_at, leg.alight_eta.basis_at) for leg in legs.values())
    return SceneRecord(
        scene_key=_digest(key_payload),
        cohort_key=cohort,
        independence_group="|".join(sorted({leg.run_key for leg in legs.values()})),
        information_at=information_at,
        static_version=static_version,
        route_context={"static_route_key": static_candidate_id, "rides": rides,
                       "timing_policy_version": policy.timing_policy_version,
                       "sample_policy_version": policy.sample_policy_version},
        predictions=predictions,
        label_deadline_at=evaluated_at + timedelta(seconds=label_deadline_seconds),
    ), None


# ---------------------------------------------------------------- DB 행 → 평가 입력

def _outcome(ctx: Mapping[str, Any], observed_at: datetime | None, match_status: str = "matched") -> PredictionOutcome:
    return PredictionOutcome(
        predicted_arrival_at=datetime.fromisoformat(ctx["predicted_arrival_at"]),
        eta_source=ctx["eta_source"],
        model_version=ctx.get("model_version"),
        horizon_seconds=ctx["horizon_seconds"],
        run_key=ctx.get("run_key"),
        observed=None if observed_at is None else ObservedEvent(observed_at=observed_at, event_type="arrival",
                                                                match_status=match_status),
    )


def transfer_error_samples(rows: Iterable[Mapping[str, Any]]) -> list[TransferErrorSample]:
    """판정 완료(labeled) 표본만 상대 오차 표본으로 바꾼다. 점 시각이 없으면 쓰지 않는다."""
    samples = []
    for row in rows:
        if row["label_status"] != "labeled" or row["front_observed_at"] is None or row["next_observed_at"] is None:
            continue
        ctx = row["prediction_context"]
        samples.append(TransferErrorSample(
            sample_id=str(row["id"]),
            run_group_key=row["independence_group"],
            condition_key=row["cohort_key"],
            timing_policy_version=ctx["timing_policy_version"],
            front=_outcome(ctx["front"] | {"predicted_arrival_at": row["front_prediction_at"].isoformat()},
                           row["front_observed_at"]),
            next=_outcome(ctx["next"] | {"predicted_arrival_at": row["next_prediction_at"].isoformat()},
                          row["next_observed_at"]),
        ))
    return samples


def joint_scenes(rows: Iterable[Mapping[str, Any]]) -> list[JointScene]:
    """완료(complete) 묶음만 공동 시나리오로 바꾼다. 관측은 (ride_index, vehicle_rank)로 예측에 대응시킨다."""
    scenes = []
    for row in rows:
        if row["status"] != "complete":
            continue
        observed = {(o["ride_index"], o["vehicle_rank"]): o for o in row["observations"]}
        rides = []
        for p in row["predictions"]:
            o = observed.get((p["ride_index"], p["vehicle_rank"]), {})
            board_at = (o.get("board") or {}).get("observed_at")
            alight_at = (o.get("alight") or {}).get("observed_at")
            rides.append(SceneRide(
                pattern_id=p["pattern_id"], board_position=p["board_position"], alight_position=p["alight_position"],
                vehicle_rank=p["vehicle_rank"],
                board=_outcome(p["board"], datetime.fromisoformat(board_at) if board_at else None),
                alight=_outcome(p["alight"], datetime.fromisoformat(alight_at) if alight_at else None),
            ))
        scenes.append(JointScene(scene_id=str(row["id"]), run_group_key=row["independence_group"],
                                 condition_key=row["cohort_key"],
                                 timing_policy_version=row["route_context"]["timing_policy_version"],
                                 rides=tuple(rides)))
    return scenes


# ---------------------------------------------------------------- 사후 판정

@dataclass
class EventMatch:
    observation_id: int
    event_at: datetime | None
    quality: str
    lower_at: datetime | None = None
    upper_at: datetime | None = None


@dataclass
class SampleLabel:
    label_status: str
    front: EventMatch | None
    next: EventMatch | None
    time_feasible: bool | None
    observation_context: dict[str, Any] = field(default_factory=dict)


def label_sample(row: Mapping[str, Any], front: EventMatch | None, following: EventMatch | None,
                 now: datetime) -> SampleLabel | None:
    """표본 하나를 판정한다. 두 차량의 도착 점 시각이 모두 확인되면 labeled, 기한이 지나면 insufficient, 아니면 None(대기)."""
    context = {
        "front": _match_context(front), "next": _match_context(following),
        "event_type": "arrival", "rule": "same pattern·stop_seq·run_key, quality=exact",
    }
    if front is not None and following is not None and front.event_at and following.event_at \
            and front.quality == "exact" and following.quality == "exact":
        feasible = front.event_at + timedelta(seconds=row["walk_seconds"] + row["buffer_seconds"]) <= following.event_at
        return SampleLabel("labeled", front, following, feasible, context)
    if now >= row["label_deadline_at"]:
        context["reason"] = "point_observation_missing_at_deadline"
        return SampleLabel("insufficient", front, following, None, context)
    return None


def _match_context(match: EventMatch | None) -> dict[str, Any] | None:
    if match is None:
        return None
    return {"observation_id": match.observation_id, "event_at": match.event_at.isoformat() if match.event_at else None,
            "quality": match.quality, "lower_at": match.lower_at.isoformat() if match.lower_at else None,
            "upper_at": match.upper_at.isoformat() if match.upper_at else None}


def label_scene(row: Mapping[str, Any], matches: Mapping[tuple[int, int, str], EventMatch | None],
                now: datetime) -> tuple[str, list[dict[str, Any]], dict[str, Any]] | None:
    """묶음의 모든 예측(구간·차량 순위)에 승차·하차 도착 점 시각이 확인되면 complete, 기한이 지나면 insufficient."""
    observations, missing = [], []
    for p in row["predictions"]:
        entry: dict[str, Any] = {"ride_index": p["ride_index"], "vehicle_rank": p["vehicle_rank"]}
        for side in ("board", "alight"):
            match = matches.get((p["ride_index"], p["vehicle_rank"], side))
            if match is not None and match.quality == "exact" and match.event_at is not None:
                entry[side] = {"observation_id": match.observation_id, "observed_at": match.event_at.isoformat(),
                               "event_type": "arrival", "match_status": "matched"}
            else:
                entry[side] = _match_context(match)
                missing.append(f"{p['ride_index']}:{p['vehicle_rank']}:{side}")
        observations.append(entry)
    if not missing:
        return "complete", observations, {"required_events": "all", "missing": []}
    if now >= row["label_deadline_at"]:
        return "insufficient", observations, {"missing": missing, "reason": "point_observation_missing_at_deadline"}
    return None
