"""환승 표본(app.transfer_samples)·공동 관측 묶음(app.journey_scenes)의 저장·조회·판정 갱신.

적용 DDL: 서버 deployment/migrations/04_transfer_samples.sql·03_journey_scenes.sql. 예측 컬럼은 생성 후 바꾸지 않고
(같은 키는 ON CONFLICT DO NOTHING으로 재사용) 사후 관측·판정 컬럼만 갱신한다. 연결·트랜잭션은 호출자가 관리한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from app.modules.evaluation.samples import SampleLabel, SampleRecord, SceneRecord


def upsert_samples(conn: psycopg.Connection, records: Sequence[SampleRecord],
                   stop_ids: dict[str, int]) -> dict[str, UUID]:
    """표본을 만들거나 같은 sample_key의 기존 표본을 재사용한다. sample_key → id."""
    for r in records:
        conn.execute(
            """
            INSERT INTO app.transfer_samples (sample_key, cohort_key, independence_group, evaluated_at,
                front_prediction_at, next_prediction_at, front_stop_id, next_stop_id, walk_seconds, buffer_seconds,
                prediction_context, predicted_probability, probability_context, label_deadline_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (sample_key) DO NOTHING
            """,
            (r.sample_key, r.cohort_key, r.independence_group, r.evaluated_at, r.front_prediction_at,
             r.next_prediction_at, stop_ids.get(r.front_stop_external_id), stop_ids.get(r.next_stop_external_id),
             r.walk_seconds, r.buffer_seconds, Jsonb(r.prediction_context), r.predicted_probability,
             Jsonb(r.probability_context), r.label_deadline_at),
        )
    keys = sorted({r.sample_key for r in records})
    if not keys:
        return {}
    return dict(conn.execute("SELECT sample_key, id FROM app.transfer_samples WHERE sample_key = ANY(%s)",
                             (keys,)).fetchall())


def upsert_scenes(conn: psycopg.Connection, records: Sequence[SceneRecord]) -> dict[str, UUID]:
    for r in records:
        conn.execute(
            """
            INSERT INTO app.journey_scenes (scene_key, cohort_key, independence_group, information_at, static_version,
                route_context, predictions, label_deadline_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (scene_key) DO NOTHING
            """,
            (r.scene_key, r.cohort_key, r.independence_group, r.information_at, r.static_version,
             Jsonb(r.route_context), Jsonb(r.predictions), r.label_deadline_at),
        )
    keys = sorted({r.scene_key for r in records})
    if not keys:
        return {}
    return dict(conn.execute("SELECT scene_key, id FROM app.journey_scenes WHERE scene_key = ANY(%s)",
                             (keys,)).fetchall())


_SAMPLE_COLUMNS = ("id", "sample_key", "cohort_key", "independence_group", "evaluated_at", "front_prediction_at",
                   "next_prediction_at", "walk_seconds", "buffer_seconds", "prediction_context", "front_observed_at",
                   "next_observed_at", "label_status", "time_feasible", "label_deadline_at")
_SCENE_COLUMNS = ("id", "scene_key", "cohort_key", "independence_group", "information_at", "route_context",
                  "predictions", "observations", "status", "label_deadline_at")


def labeled_samples(conn: psycopg.Connection, cohort_key: str, before: datetime) -> list[dict[str, Any]]:
    """비교 가능한 조건의 판정 완료 표본. 평가 기준시각 이전에 판정된 것만 쓴다(사후 정보 누설 방지)."""
    rows = conn.execute(
        f"SELECT {', '.join(_SAMPLE_COLUMNS)} FROM app.transfer_samples "
        "WHERE cohort_key = %s AND label_status = 'labeled' AND labeled_at <= %s",
        (cohort_key, before),
    ).fetchall()
    return [dict(zip(_SAMPLE_COLUMNS, row)) for row in rows]


def complete_scenes(conn: psycopg.Connection, cohort_key: str, before: datetime) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {', '.join(_SCENE_COLUMNS)} FROM app.journey_scenes "
        "WHERE cohort_key = %s AND status = 'complete' AND completed_at <= %s",
        (cohort_key, before),
    ).fetchall()
    return [dict(zip(_SCENE_COLUMNS, row)) for row in rows]


def pending_samples(conn: psycopg.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {', '.join(_SAMPLE_COLUMNS)} FROM app.transfer_samples WHERE label_status = 'pending' "
        "ORDER BY label_deadline_at, id LIMIT %s FOR UPDATE SKIP LOCKED",
        (limit,),
    ).fetchall()
    return [dict(zip(_SAMPLE_COLUMNS, row)) for row in rows]


def pending_scenes(conn: psycopg.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {', '.join(_SCENE_COLUMNS)} FROM app.journey_scenes WHERE status = 'pending' "
        "ORDER BY label_deadline_at, id LIMIT %s FOR UPDATE SKIP LOCKED",
        (limit,),
    ).fetchall()
    return [dict(zip(_SCENE_COLUMNS, row)) for row in rows]


def apply_sample_label(conn: psycopg.Connection, sample_id: UUID, label: SampleLabel, now: datetime) -> None:
    conn.execute(
        """
        UPDATE app.transfer_samples
           SET label_status = %s, time_feasible = %s, front_event_observation_id = %s,
               next_event_observation_id = %s, front_observed_at = %s, next_observed_at = %s,
               observation_context = %s, labeled_at = %s
         WHERE id = %s AND label_status = 'pending'
        """,
        (label.label_status, label.time_feasible,
         label.front.observation_id if label.front else None, label.next.observation_id if label.next else None,
         label.front.event_at if label.front and label.label_status == "labeled" else None,
         label.next.event_at if label.next and label.label_status == "labeled" else None,
         Jsonb(label.observation_context), now if label.label_status == "labeled" else None, sample_id),
    )


def apply_scene_label(conn: psycopg.Connection, scene_id: UUID, status: str, observations: list[dict[str, Any]],
                      quality: dict[str, Any], now: datetime) -> None:
    conn.execute(
        "UPDATE app.journey_scenes SET status = %s, observations = %s, quality = %s, completed_at = %s "
        "WHERE id = %s AND status = 'pending'",
        (status, Jsonb(observations), Jsonb(quality), now if status == "complete" else None, scene_id),
    )
