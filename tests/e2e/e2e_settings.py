"""E2E 실행 설정. 정책 값은 사용자가 승인한 Phase 3 값(.env.api)과 같은 크기를 쓰되, 합성 월드 시험을 위해 후보 상한만
넓히고 재평가·WebSocket 확인 간격을 짧게 한다(실행 시간 단축용, 운영 값이 아님)."""

from app.core.config import Settings

POLICY = dict(
    search_max_boardings=2, search_access_radius_meters=300, search_transfer_radius_meters=150,
    search_egress_radius_meters=300, search_max_candidates=50, search_max_states=100000, search_budget_ms=5000,
    eval_search_horizon_seconds=3600, eval_budget_ms=5000, eval_max_timed_candidates=300,
    eval_boarding_buffer_seconds=60, eval_max_data_age_seconds=900, eval_min_segment_samples=5,
    eval_min_probability_samples=30, eval_min_joint_scenes=30, eval_min_improvement_seconds=120,
    eval_max_horizon_difference_seconds=300, eval_max_recovery_attempts=2, eval_recovery_search_enabled=False,
    eval_timing_policy_version="timing-v1", eval_sample_policy_version="sample-v1",
    eval_recovery_policy_version="recovery-v1", eval_segment_model_version="segment-v1",
    search_expires_seconds=1800, tmap_search_option="0", tmap_max_lookups_per_search=60,
    sample_label_deadline_seconds=7200,
)
TIMING = dict(job_poll_seconds=0.5, journey_recalc_interval_seconds=60, ws_poll_seconds=0.5,
              ws_auth_timeout_seconds=5, worker_poll_seconds=5)


def e2e_settings(**extra) -> Settings:
    return Settings(_env_file=None, **POLICY, **TIMING, **extra)
